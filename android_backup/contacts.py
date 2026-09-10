"""联系人备份/恢复实现。

备份（需 root）：
1. 读取设备属性（Android 版本、SDK、fingerprint），写入 manifest 供后续跨版本警告。
2. 停止联系人 Provider，root tar 打包 databases 整个目录
   到 /data/local/tmp/contacts_databases_<ts>.tar，然后 adb pull 到本地。
3. 读取 tar 中每个 .db 文件的 sqlite3 PRAGMA user_version 写入 manifest。

恢复（需 root）：
1. 读 manifest 中的源设备版本，对比目标设备当前 Android 版本：
   - 主版本一致：直接继续
   - 不一致：输出 show_warning 并询问是否继续（通过 select_items 式的单步确认）
2. **预备份目标设备现有数据库**：在写入任何东西前，把目标设备上的 databases 先
   tar 一份拉回本地 contacts/_pre_restore_backup_<ts>.tar，保证可手动回滚。
3. `am force-stop com.android.providers.contacts` 释放 DB 锁。
4. 先把原目录移为远端临时备份，再推送、校验并解包 tar。
5. 修复 owner/mode/SELinux；解包或修复失败则自动回滚原目录。
6. 再次 `am force-stop` provider 让其重新载入。
"""

from __future__ import annotations

import datetime as _dt
import io
import json
import logging
import sqlite3
import tarfile
import time
from pathlib import Path, PurePosixPath
from typing import Dict, Optional, Tuple

from . import adb, ui

logger = logging.getLogger("android_backup.contacts")

ITEM = {
    "id": "contacts",
    "label": "联系人（含群组/头像/通话记录等）",
    "files": ["contacts/manifest.json", "contacts/contacts_databases.tar"],
}

PKG = "com.android.providers.contacts"
PKG_ROOT = f"/data/data/{PKG}"
SUB_DIR = "databases"
TAR_NAME = "contacts_databases.tar"


# -------------------------- 辅助函数 --------------------------

def _human_size(n: int) -> str:
    step = 1024
    for unit in ("B", "KB", "MB", "GB"):
        if n < step:
            return f"{n:.0f} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} TB"


def _ensure_root() -> None:
    """若当前 adb 无 root 权限，抛异常（由顶层 item requires_root 先行禁用，
    这里是双保险）。"""
    if not adb.has_root_access():
        raise RuntimeError(
            "联系人备份需要 root 权限：当前 adb shell 非 root，无法访问 "
            f"{PKG_ROOT}/databases。请先对设备 root 后重试。"
        )


def _package_exists(pkg: str) -> bool:
    """检查设备上是否存在该包（pm path）。"""
    proc = adb.run_adb(["shell", f"pm path {pkg} 2>/dev/null"], check=False)
    return proc.returncode == 0 and "package:" in proc.stdout


def _get_device_info() -> Dict[str, str]:
    """读 getprop 返回 {release, sdk, fingerprint}。"""
    return {
        "release": adb.getprop("ro.build.version.release") or "?",
        "sdk": adb.getprop("ro.build.version.sdk") or "?",
        "fingerprint": adb.getprop("ro.build.fingerprint") or "?",
        "brand": adb.getprop("ro.product.brand") or "?",
        "model": adb.getprop("ro.product.model") or "?",
    }


def _remote_exists(path: str) -> bool:
    proc = adb.run_adb(
        ["shell", f"[ -e '{path}' ] && echo YES || echo NO"], check=False
    )
    return proc.returncode == 0 and "YES" in proc.stdout


def _tar_remote_dir(pkg_root: str, sub_dir: str,
                    tmp_remote_tar: str) -> Optional[int]:
    """在远端把 <pkg_root>/<sub_dir> tar 打包到 tmp_remote_tar。

    返回 tar 的字节数；目录不存在返回 None，打包失败则抛异常。
    """
    target = f"{pkg_root}/{sub_dir}"
    if not _remote_exists(target):
        return None
    # 解析真实路径（防御 symlink）
    real_proc = adb.run_adb(
        ["shell", f"readlink -f {target}"], check=False
    )
    real = (real_proc.stdout.strip()
            if real_proc.returncode == 0 and real_proc.stdout.strip()
            else target)
    parent = real.rsplit("/", 1)[0] or "/"
    base = real.rsplit("/", 1)[1]
    cmd = f"cd '{parent}' && tar -cpf '{tmp_remote_tar}' '{base}'"
    proc = adb.run_adb(["shell", cmd], check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"tar 打包失败 {real}: "
            f"{proc.stderr.strip() or proc.stdout.strip() or f'exit={proc.returncode}'}"
        )
    adb.shell(f"chmod 644 '{tmp_remote_tar}'", check=True)
    if not _remote_exists(tmp_remote_tar):
        raise RuntimeError(f"tar 命令成功但未生成文件: {tmp_remote_tar}")
    sz_proc = adb.run_adb(
        ["shell", f"stat -c %s '{tmp_remote_tar}'"], check=False
    )
    try:
        size = int(sz_proc.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"无法读取远端 tar 大小: {tmp_remote_tar}") from exc
    if size <= 0:
        raise RuntimeError(f"远端 tar 为空: {tmp_remote_tar}")
    return size


def _push_untar_remote(local_tar: Path, tmp_remote: str,
                       pkg_root: str, sub_dir: str) -> Optional[str]:
    """把本地 tar 推到 tmp_remote，解覆盖 pkg_root/sub_dir。

    步骤：
    - adb push local_tar → tmp_remote
    - cd pkg_root; 先 mv sub_dir 到 .bak_<ts>
    - tar -xpf tmp_remote → 此时会在 pkg_root/sub_dir 解出内容
    - 解包失败立即回滚；成功时返回 .bak 路径供权限修复后提交
    """
    if not local_tar.exists():
        raise FileNotFoundError(f"本地 tar 不存在: {local_tar}")
    _validate_restore_tar(local_tar, sub_dir)
    adb.push(local_tar, tmp_remote)
    try:
        ts = _dt.datetime.now().strftime("%H%M%S%f")
        bak = f"{pkg_root}/{sub_dir}.bak_{ts}"
        target = f"{pkg_root}/{sub_dir}"
        moved = "MOVED" in adb.shell(
            f"if [ -e '{target}' ]; then mv '{target}' '{bak}' && echo MOVED; "
            f"else echo EMPTY; fi",
            check=True,
        )
        try:
            adb.shell(f"cd '{pkg_root}' && tar -xpf '{tmp_remote}'", check=True)
        except Exception:
            adb.shell(f"rm -rf '{target}'", check=False)
            if moved:
                adb.shell(f"mv '{bak}' '{target}'", check=True)
            raise
        return bak if moved else None
    finally:
        adb.shell(f"rm -f '{tmp_remote}'", check=False)


def _rollback_remote_dir(pkg_root: str, sub_dir: str,
                         backup_dir: Optional[str]) -> None:
    """权限修复或验证失败时，恢复刚才移走的原目录。"""
    target = f"{pkg_root}/{sub_dir}"
    adb.shell(f"rm -rf '{target}'", check=False)
    if backup_dir:
        adb.shell(f"mv '{backup_dir}' '{target}'", check=True)


def _discard_remote_backup(backup_dir: Optional[str]) -> None:
    if backup_dir:
        adb.shell(f"rm -rf '{backup_dir}'", check=False)


def _validate_restore_tar(local_tar: Path, expected_root: str) -> None:
    """拒绝越界路径和链接，避免损坏/被篡改的 tar 写出数据目录。"""
    with tarfile.open(local_tar, "r") as tf:
        members = tf.getmembers()
        if not members:
            raise ValueError(f"备份 tar 为空: {local_tar}")
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts
                    or path.parts[0] != expected_root):
                raise ValueError(f"tar 包含非法路径: {member.name!r}")
            if member.issym() or member.islnk():
                raise ValueError(f"tar 包含不允许的链接: {member.name!r}")


def _fix_owner_selinux(pkg_root: str, sub_dir: str) -> Tuple[str, str]:
    """chown + chmod + 双跑 restorecon。返回 (owner_uid, 最后 ls -ldZ 输出)供日志。"""
    # 读 pkg_root 本身的 uid/gid 作为 databases 的 owner
    stat = adb.run_adb(
        ["shell", f"stat -c '%u:%g' '{pkg_root}'"], check=False
    )
    if stat.returncode != 0 or not stat.stdout.strip():
        raise RuntimeError(
            f"无法读取 {pkg_root} 的 owner uid/gid（stat 失败）"
        )
    ug = stat.stdout.strip()
    target = f"{pkg_root}/{sub_dir}"
    # 顶层目录权限 0771，文件 0660
    adb.shell(f"chmod 0771 '{target}'", check=False)
    adb.shell(f"find '{target}' -type d -exec chmod 0771 {{}} +", check=False)
    adb.shell(f"find '{target}' -type f -exec chmod 0660 {{}} +", check=False)
    # 用 find -exec chown，避免 toybox chown -R 在特殊文件上中断
    adb.shell(f"find '{target}' -exec chown {ug} {{}} +", check=False)
    # 双跑 restorecon
    adb.shell(f"restorecon -RF '{target}'", check=False)
    adb.shell(f"restorecon -RF '{target}'", check=False)
    z_proc = adb.run_adb(
        ["shell", f"ls -ldZ '{target}'"], check=False
    )
    z_info = z_proc.stdout.strip() if z_proc.returncode == 0 else ""
    logger.info("修复后 %s: owner=%s  context=%s", target, ug, z_info)
    return ug, z_info


def _stop_provider() -> None:
    """force-stop 联系人 provider；核心 Provider 停止失败时抛异常。"""
    for extra in ("com.android.contacts",
                  "com.google.android.contacts"):
        adb.shell(f"am force-stop {extra}", check=False)
    adb.shell(f"am force-stop {PKG}", check=True)


def _read_schema_versions(local_tar: Path) -> Dict[str, int]:
    """从本地 tar 包中提取 *.db 文件，每个读 PRAGMA user_version 返回 dict。"""
    schemas: Dict[str, int] = {}
    try:
        with tarfile.open(local_tar, "r") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                name = Path(member.name).name
                if not name.endswith(".db"):
                    continue
                data = tf.extractfile(member)
                if data is None:
                    continue
                buf = data.read()
                try:
                    con = sqlite3.connect(f"file:memdb1?mode=memory&cache=shared",
                                          uri=True)
                    try:
                        con.execute("ATTACH DATABASE ':memory:' AS attached")
                        # 直接把 buf 写到临时文件更稳妥
                        import tempfile
                        import os
                        with tempfile.NamedTemporaryFile(delete=False,
                                                         suffix=".db") as tmp:
                            tmp.write(buf)
                            tmp_path = tmp.name
                        try:
                            with sqlite3.connect(tmp_path) as c:
                                cur = c.execute("PRAGMA user_version")
                                ver = cur.fetchone()
                                schemas[name] = int(ver[0]) if ver else 0
                        finally:
                            try:
                                os.unlink(tmp_path)
                            except OSError:
                                pass
                    finally:
                        con.close()
                except sqlite3.Error as exc:
                    logger.warning("读取 sqlite schema 失败 %s: %s", name, exc)
                    schemas[name] = -1
    except (tarfile.TarError, OSError) as exc:
        logger.warning("打开 tar 读 schema 失败: %s", exc)
    return schemas


# -------------------------- 备份 --------------------------

def backup(dest_dir: Path) -> None:
    _ensure_root()
    if not _package_exists(PKG):
        # 没有标准联系人 provider（罕见）——发出 warning 但继续，以进度行形式告知
        ui.progress_start("检查联系人 Provider 包")
        ui.progress_done("检查联系人 Provider 包", False,
                         f"设备上未找到 {PKG}（厂商可能使用自定义包），"
                         f"备份内容可能为空或不完整")
    else:
        ui.progress_start("检查联系人 Provider 包")
        ui.progress_done("检查联系人 Provider 包", True, f"已找到 {PKG}")

    # 阶段 1：读取设备信息
    ui.progress_start("读取设备版本信息")
    info = _get_device_info()
    detail = (f"Android {info['release']} / SDK {info['sdk']} / "
              f"{info['brand']} {info['model']}")
    ui.progress_done("读取设备版本信息", True, detail)

    ts = _dt.datetime.now().strftime("%H%M%S%f")
    remote_tar = f"/data/local/tmp/contacts_bak_{ts}.tar"

    # 先停止 Provider，确保主库/WAL/SHM 来自同一静态时点。
    ui.progress_start("停止联系人 Provider（创建一致性快照）")
    _stop_provider()
    time.sleep(0.8)
    ui.progress_done("停止联系人 Provider（创建一致性快照）", True)

    # 阶段 2：打包
    ui.progress_start("打包 contacts databases (tar)")
    try:
        size = _tar_remote_dir(PKG_ROOT, SUB_DIR, remote_tar)
    except Exception as exc:
        ui.progress_done("打包 contacts databases (tar)", False, str(exc))
        adb.shell(f"rm -f '{remote_tar}'", check=False)
        raise
    if size is None:
        ui.progress_done("打包 contacts databases (tar)", False,
                         f"{PKG_ROOT}/{SUB_DIR} 不存在或打包失败")
        # 清理
        adb.shell(f"rm -f '{remote_tar}'", check=False)
        raise RuntimeError(
            f"打包联系人数据库失败：{PKG_ROOT}/{SUB_DIR} 不存在或不可访问"
        )
    ui.progress_done("打包 contacts databases (tar)", True, _human_size(size))

    # 阶段 3：拉取
    contacts_root = dest_dir / "contacts"
    contacts_root.mkdir(parents=True, exist_ok=True)
    local_tar = contacts_root / TAR_NAME
    ui.progress_start("拉取 contacts_databases.tar")
    t0 = time.time()
    try:
        adb.pull(remote_tar, local_tar)
    finally:
        adb.shell(f"rm -f '{remote_tar}'", check=False)
    elapsed = time.time() - t0
    ui.progress_done("拉取 contacts_databases.tar", True,
                     f"用时 {elapsed:.1f}s, {_human_size(local_tar.stat().st_size)}")

    # 阶段 4：schema + manifest
    ui.progress_start("解析 DB schema 并写 manifest")
    schemas = _read_schema_versions(local_tar)
    manifest = {
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "source_package": PKG,
        "source_device": info,
        "tar_file": TAR_NAME,
        "tar_size_bytes": local_tar.stat().st_size,
        "db_schemas": schemas,
    }
    (contacts_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if schemas:
        first_name, first_ver = next(iter(schemas.items()))
        detail = f"{len(schemas)} 个 db，主库 {first_name} schema={first_ver}"
    else:
        detail = "未能读取 schema（可能数据库为空或异常）"
    ui.progress_done("解析 DB schema 并写 manifest", True, detail)


# -------------------------- 恢复 --------------------------

def _confirm_version_mismatch(src_release: str, src_sdk: str,
                               dst_release: str, dst_sdk: str) -> bool:
    """跨版本时弹 warning + 单步确认。返回 True 表示用户确认继续。"""
    ui.show_warning(
        f"⚠️ 联系人备份来自 Android {src_release}（SDK {src_sdk}），"
        f"当前目标设备是 Android {dst_release}（SDK {dst_sdk}）。"
        f"不同 Android 版本的联系人数据库 schema 可能不兼容，"
        f"直接覆盖可能导致系统联系人 App 无法启动或丢失数据。"
        f"本工具已自动预备份目标设备现有联系人数据库到 "
        f"本地 contacts/_pre_restore_backup_*.tar 供回滚。"
    )
    # 用 select_items 做单步确认
    chosen = ui.select_items(
        [{"id": "yes", "label": "我了解风险，仍然恢复联系人数据库（可能需手动回滚）"}],
        title="跨 Android 版本恢复联系人：请确认是否继续",
        default_all=False,
    )
    return "yes" in chosen


def restore(src_dir: Path) -> None:
    _ensure_root()
    contacts_root = src_dir / "contacts"
    if not contacts_root.exists():
        raise FileNotFoundError(f"contacts 目录不存在: {contacts_root}")
    manifest_path = contacts_root / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"未找到 contacts/manifest.json: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tar_name = manifest.get("tar_file") or TAR_NAME
    if tar_name != TAR_NAME:
        raise ValueError(f"contacts manifest 中 tar_file 非法: {tar_name!r}")
    src_tar = contacts_root / tar_name
    if not src_tar.exists():
        raise FileNotFoundError(f"备份 tar 不存在: {src_tar}")

    # 阶段 A：兼容性检查
    src = manifest.get("source_device") or {}
    src_release = src.get("release", "?")
    src_sdk = src.get("sdk", "?")
    dst = _get_device_info()
    dst_release = dst.get("release", "?")
    dst_sdk = dst.get("sdk", "?")
    ui.progress_start("检查 Android 版本兼容性")
    mismatch = (src_sdk != "?" and dst_sdk != "?" and src_sdk != dst_sdk)
    if mismatch:
        ui.progress_done("检查 Android 版本兼容性", True,
                         f"源={src_release}(SDK {src_sdk})  目标={dst_release}(SDK {dst_sdk})  — 不一致")
        if not _confirm_version_mismatch(src_release, src_sdk, dst_release, dst_sdk):
            logger.warning("用户取消跨版本联系人恢复")
            raise RuntimeError("用户在跨版本警告下取消了联系人恢复")
    else:
        ui.progress_done("检查 Android 版本兼容性", True,
                         f"源=目标={dst_release}(SDK {dst_sdk})")

    # 阶段 B：先停止 Provider，再预备份目标设备现有数据库。
    ui.progress_start("停止联系人 Provider 进程（释放 DB 锁）")
    _stop_provider()
    time.sleep(0.8)
    ui.progress_done("停止联系人 Provider 进程（释放 DB 锁）", True)

    ts = _dt.datetime.now().strftime("%Y%m%d%H%M%S")
    pre_basename = f"_pre_restore_backup_{ts}.tar"
    pre_remote = f"/data/local/tmp/{pre_basename}"
    pre_local = contacts_root / pre_basename
    ui.progress_start("预备份目标设备现有联系人数据库")
    try:
        pre_size = _tar_remote_dir(PKG_ROOT, SUB_DIR, pre_remote)
        if pre_size is None:
            # 目标没有 databases（新设备）—— 也算正常
            adb.shell(f"rm -f '{pre_remote}'", check=False)
            ui.progress_done("预备份目标设备现有联系人数据库", True,
                             "目标暂无 contacts databases（新机），无需备份")
        else:
            adb.pull(pre_remote, pre_local)
            adb.shell(f"rm -f '{pre_remote}'", check=False)
            ui.progress_done("预备份目标设备现有联系人数据库", True,
                             f"{pre_basename}  ({_human_size(pre_local.stat().st_size)})"
                             f"  — 如需回滚请手动恢复此文件")
    except Exception as exc:  # noqa: BLE001
        adb.shell(f"rm -f '{pre_remote}'", check=False)
        ui.progress_done("预备份目标设备现有联系人数据库", False, str(exc))
        ui.show_warning(
            "目标设备联系人预备份失败！继续恢复将无法回滚。"
            "建议取消并手动备份后重试。"
        )
        cancel_items = ui.select_items(
            [{"id": "cancel", "label": "取消联系人恢复（推荐）"},
             {"id": "continue", "label": "忽略预备份失败，仍继续恢复（风险自负）"}],
            title="预备份失败，是否继续？",
            default_all=False,
        )
        if "cancel" in cancel_items or "continue" not in cancel_items:
            raise RuntimeError("用户在预备份失败后取消联系人恢复")

    # 阶段 C：推送并解包
    tmp_remote = f"/data/local/tmp/_restore_contacts_{ts}.tar"
    ui.progress_start("推送并解包 databases")
    remote_backup: Optional[str] = None
    try:
        remote_backup = _push_untar_remote(src_tar, tmp_remote, PKG_ROOT, SUB_DIR)
    except Exception as exc:  # noqa: BLE001
        adb.shell(f"rm -f '{tmp_remote}'", check=False)
        ui.progress_done("推送并解包 databases", False, str(exc))
        raise
    ui.progress_done("推送并解包 databases", True)

    # 阶段 E：权限 + SELinux
    ui.progress_start("修复 owner/permission/SELinux 标签")
    try:
        ug, z_info = _fix_owner_selinux(PKG_ROOT, SUB_DIR)
    except Exception as exc:  # noqa: BLE001
        ui.progress_done("修复 owner/permission/SELinux 标签", False, str(exc))
        try:
            _rollback_remote_dir(PKG_ROOT, SUB_DIR, remote_backup)
        except Exception:  # noqa: BLE001
            logger.exception("权限修复失败后自动回滚联系人数据库也失败")
        raise
    detail = f"owner={ug}"
    if z_info:
        detail += f"  context='{z_info.strip()[-80:]}'"
    ui.progress_done("修复 owner/permission/SELinux 标签", True, detail)
    _discard_remote_backup(remote_backup)

    # 阶段 F：重启 provider
    ui.progress_start("重启联系人 Provider（让数据库生效）")
    _stop_provider()
    ui.progress_done("重启联系人 Provider（让数据库生效）", True,
                     "已 force-stop；下次打开「联系人」App 会自动加载")

    ui.show_warning(
        "联系人恢复已完成。如「联系人」App 出现闪退，请先尝试「设置 → 应用 → 联系人"
        " → 存储 → 清空数据」后重试；若仍不可用，请手动使用 "
        f"`{contacts_root / pre_basename}` 预备份文件进行回滚。"
    )
