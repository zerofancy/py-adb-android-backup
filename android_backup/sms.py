"""短信与彩信 备份/恢复实现。

整体结构与 contacts.py 高度相似，区别在于：
  - 包名 = com.android.providers.telephony
  - 备份两个子目录：databases (mmssms.db 等) + app_parts (彩信附件二进制)
  - 恢复时 force-stop 额外的短信 App（com.google.android.apps.messaging、
    com.android.mms、com.miui.smsextra 等）
  - manifest 字段 includes_app_parts 指示是否含附件包

备份（需 root）：
    tar databases → mmssms_databases.tar
    (可选) tar app_parts → app_parts.tar
    写 manifest

恢复（需 root）：
    版本警告 → 预备份 → force-stop 短信相关进程 → push & untar →
    chown + restorecon → force-stop
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import tarfile
import time
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional

from . import adb, ui

logger = logging.getLogger("android_backup.sms")

ITEM = {
    "id": "sms",
    "label": "短信与彩信（含会话、附件）",
    "files": [
        "sms/manifest.json",
        "sms/mmssms_databases.tar",
        "sms/app_parts.tar（可选）",
    ],
}

PKG = "com.android.providers.telephony"
PKG_ROOT = f"/data/data/{PKG}"

SUB_DIRS = [
    # (子目录名, 本地 tar 文件名, 是否必须)
    ("databases", "mmssms_databases.tar", True),
    ("app_parts", "app_parts.tar", False),
]

# 额外需要 force-stop 的短信应用包名（force-stop 失败静默）
EXTRA_SMS_PACKAGES = [
    "com.android.mms",
    "com.google.android.apps.messaging",
    "com.miui.smsextra",
    "com.miui.mms",
    "com.oneplus.mms",
    "com.oppo.mms",
    "com.huawei.android.mms",
    "com.samsung.android.messaging",
    PKG,
]


# -------------------------- 辅助函数（同 contacts.py，直接使用 adb/contacts 现有能力）
#
# 为避免跨模块强耦合，这里把 contacts 内部的通用工具以本地函数形式复制一份
# （都是 10 行以下的薄封装）。这样 sms.py 可以独立 import 运行。
# -------------------------------------------------------------------------------------

def _human_size(n: int) -> str:
    step = 1024
    for unit in ("B", "KB", "MB", "GB"):
        if n < step:
            return f"{n:.0f} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} TB"


def _ensure_root() -> None:
    if not adb.has_root_access():
        raise RuntimeError(
            "短信备份需要 root 权限：当前 adb shell 非 root，无法访问 "
            f"{PKG_ROOT}/databases。请先对设备 root 后重试。"
        )


def _package_exists(pkg: str) -> bool:
    proc = adb.run_adb(["shell", f"pm path {pkg} 2>/dev/null"], check=False)
    return proc.returncode == 0 and "package:" in proc.stdout


def _get_device_info() -> Dict[str, str]:
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
                    tmp_remote_tar: str) -> int | None:
    target = f"{pkg_root}/{sub_dir}"
    if not _remote_exists(target):
        return None
    real_proc = adb.run_adb(["shell", f"readlink -f {target}"], check=False)
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
    target = f"{pkg_root}/{sub_dir}"
    adb.shell(f"rm -rf '{target}'", check=False)
    if backup_dir:
        adb.shell(f"mv '{backup_dir}' '{target}'", check=True)


def _discard_remote_backup(backup_dir: Optional[str]) -> None:
    if backup_dir:
        adb.shell(f"rm -rf '{backup_dir}'", check=False)


def _validate_restore_tar(local_tar: Path, expected_root: str) -> None:
    """拒绝越界路径和链接，避免损坏/被篡改的 tar 写出 Provider 目录。"""
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


def _fix_owner_selinux(pkg_root: str, sub_dir: str) -> tuple[str, str]:
    stat = adb.run_adb(
        ["shell", f"stat -c '%u:%g' '{pkg_root}'"], check=False
    )
    if stat.returncode != 0 or not stat.stdout.strip():
        raise RuntimeError(
            f"无法读取 {pkg_root} 的 owner uid/gid（stat 失败）"
        )
    ug = stat.stdout.strip()
    target = f"{pkg_root}/{sub_dir}"
    # tar -p 已保留源文件 mode；app_parts 和 databases 的 mode 并不相同，
    # 不再统一 chmod。restorecon 只修复 SELinux 标签，不会修改 Unix mode。
    adb.shell(f"find '{target}' -exec chown {ug} {{}} + 2>/dev/null",
              check=False)
    adb.shell(f"restorecon -RF '{target}' 2>/dev/null", check=False)
    adb.shell(f"restorecon -RF '{target}' 2>/dev/null", check=False)
    z_proc = adb.run_adb(["shell", f"ls -ldZ '{target}'"], check=False)
    z_info = z_proc.stdout.strip() if z_proc.returncode == 0 else ""
    logger.info("修复后 %s: owner=%s  context=%s", target, ug, z_info)
    return ug, z_info


def _stop_all_sms_processes() -> None:
    for p in EXTRA_SMS_PACKAGES:
        if p != PKG:
            adb.shell(f"am force-stop {p}", check=False)
    # 核心 Provider 停止失败则不能声称已创建静态快照。
    adb.shell(f"am force-stop {PKG}", check=True)


# -------------------------- 备份 --------------------------

def backup(dest_dir: Path) -> None:
    _ensure_root()

    ui.progress_start("检查短信 Provider 包")
    if not _package_exists(PKG):
        ui.progress_done("检查短信 Provider 包", False,
                         f"设备上未找到 {PKG}（厂商可能使用自定义包），"
                         f"备份的短信内容可能为空或不完整")
    else:
        ui.progress_done("检查短信 Provider 包", True, f"已找到 {PKG}")

    # 阶段 1：设备信息
    ui.progress_start("读取设备版本信息")
    info = _get_device_info()
    detail = (f"Android {info['release']} / SDK {info['sdk']} / "
              f"{info['brand']} {info['model']}")
    ui.progress_done("读取设备版本信息", True, detail)

    sms_root = dest_dir / "sms"
    sms_root.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now().strftime("%H%M%S%f")

    # 停止 Provider 与短信 App，确保 DB/WAL/SHM 处于同一静态时点。
    ui.progress_start("停止短信 Provider（创建一致性快照）")
    _stop_all_sms_processes()
    time.sleep(0.8)
    ui.progress_done("停止短信 Provider（创建一致性快照）", True)

    sub_info: List[Dict] = []  # 给 manifest 用
    total_size = 0

    # 阶段 2 & 3：每个子目录 tar + 拉取
    for sub_dir, tar_name, required in SUB_DIRS:
        step_name = (f"打包 {sub_dir}/ (tar)" if sub_dir != "databases"
                     else f"打包 {sub_dir}/ (mmssms.db 等)")
        ui.progress_start(step_name)
        remote_tar = f"/data/local/tmp/sms_bak_{sub_dir}_{ts}.tar"
        try:
            size = _tar_remote_dir(PKG_ROOT, sub_dir, remote_tar)
        except Exception as exc:
            adb.shell(f"rm -f '{remote_tar}'", check=False)
            ui.progress_done(step_name, False, str(exc))
            raise
        if size is None:
            adb.shell(f"rm -f '{remote_tar}'", check=False)
            if required:
                ui.progress_done(step_name, False,
                                 f"{PKG_ROOT}/{sub_dir} 不存在或打包失败")
                raise RuntimeError(
                    f"打包短信 {sub_dir} 失败：{PKG_ROOT}/{sub_dir} 不存在或不可访问"
                )
            else:
                # app_parts 不存在是正常现象（无彩信）
                ui.progress_done(step_name, True,
                                 f"跳过 {sub_dir}（目录不存在，未收到彩信或未启用）")
                sub_info.append({
                    "sub_dir": sub_dir,
                    "tar_file": None,
                    "included": False,
                    "size_bytes": 0,
                })
                continue

        ui.progress_done(step_name, True, _human_size(size))

        step_pull = f"拉取 {tar_name}"
        ui.progress_start(step_pull)
        t0 = time.time()
        local_tar = sms_root / tar_name
        try:
            adb.pull(remote_tar, local_tar)
        finally:
            adb.shell(f"rm -f '{remote_tar}'", check=False)
        elapsed = time.time() - t0
        real_size = local_tar.stat().st_size
        total_size += real_size
        ui.progress_done(step_pull, True,
                         f"用时 {elapsed:.1f}s, {_human_size(real_size)}")
        sub_info.append({
            "sub_dir": sub_dir,
            "tar_file": tar_name,
            "included": True,
            "size_bytes": real_size,
        })

    # 阶段 4：写 manifest
    ui.progress_start("写 sms/manifest.json")
    manifest = {
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "source_package": PKG,
        "source_device": info,
        "total_size_bytes": total_size,
        "sub_dirs": sub_info,
    }
    (sms_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    included_dirs = [s["sub_dir"] for s in sub_info if s["included"]]
    ui.progress_done("写 sms/manifest.json", True,
                     f"共 {len(included_dirs)} 个子目录: {', '.join(included_dirs)}")


# -------------------------- 恢复 --------------------------

def _confirm_version_mismatch(src_release: str, src_sdk: str,
                               dst_release: str, dst_sdk: str) -> bool:
    ui.show_warning(
        f"⚠️ 短信备份来自 Android {src_release}（SDK {src_sdk}），"
        f"当前目标设备是 Android {dst_release}（SDK {dst_sdk}）。"
        f"不同 Android 版本的 mmssms.db schema 可能不兼容，"
        f"直接覆盖可能导致短信 App 崩溃或会话错乱。"
        f"本工具已自动预备份目标设备现有短信数据库到 "
        f"本地 sms/_pre_restore_backup_*.tar 供回滚。"
    )
    chosen = ui.select_items(
        [{"id": "yes",
          "label": "我了解风险，仍然恢复短信/彩信数据库（可能需手动回滚）"}],
        title="跨 Android 版本恢复短信：请确认是否继续",
        default_all=False,
    )
    return "yes" in chosen


def restore(src_dir: Path) -> None:
    _ensure_root()
    sms_root = src_dir / "sms"
    if not sms_root.exists():
        raise FileNotFoundError(f"sms 目录不存在: {sms_root}")
    manifest_path = sms_root / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"未找到 sms/manifest.json: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sub_dirs_meta: List[Dict] = manifest.get("sub_dirs") or []
    if not sub_dirs_meta:
        raise RuntimeError("sms manifest 中无 sub_dirs 条目，无法恢复")
    entries_by_subdir = {entry.get("sub_dir"): entry for entry in sub_dirs_meta}
    for sub_dir, tar_name, required in SUB_DIRS:
        entry = entries_by_subdir.get(sub_dir)
        if required and (not entry or not entry.get("included")
                         or entry.get("tar_file") != tar_name):
            raise RuntimeError(f"sms manifest 缺少必需的 {sub_dir} 备份")

    # 阶段 A：兼容性
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
                         f"源={src_release}(SDK {src_sdk})  "
                         f"目标={dst_release}(SDK {dst_sdk})  — 不一致")
        if not _confirm_version_mismatch(src_release, src_sdk, dst_release,
                                          dst_sdk):
            raise RuntimeError("用户在跨版本警告下取消了短信恢复")
    else:
        ui.progress_done("检查 Android 版本兼容性", True,
                         f"源=目标={dst_release}(SDK {dst_sdk})")

    # 阶段 B：先停止相关进程，再预备份目标数据。
    ui.progress_start("停止系统 Provider 与短信应用进程（释放 DB 锁）")
    _stop_all_sms_processes()
    time.sleep(0.8)
    ui.progress_done("停止系统 Provider 与短信应用进程（释放 DB 锁）", True,
                     f"已尝试 {len(EXTRA_SMS_PACKAGES)} 个包")

    ts = _dt.datetime.now().strftime("%Y%m%d%H%M%S")
    ui.progress_start("预备份目标设备现有短信数据库 + app_parts")
    any_pre_ok = False
    pre_error: Optional[Exception] = None
    for sub_dir, tar_name, required in SUB_DIRS:
        # 先 tar 再 pull 到 sms/_pre_restore_<ts>_<sub_dir>.tar
        remote_tar = f"/data/local/tmp/_sms_pre_{ts}_{sub_dir}.tar"
        local_tar = sms_root / f"_pre_restore_backup_{ts}_{sub_dir}.tar"
        try:
            size = _tar_remote_dir(PKG_ROOT, sub_dir, remote_tar)
            if size is None:
                adb.shell(f"rm -f '{remote_tar}'", check=False)
                continue
            adb.pull(remote_tar, local_tar)
            any_pre_ok = True
        except Exception as exc:  # noqa: BLE001
            pre_error = exc
            break
        finally:
            adb.shell(f"rm -f '{remote_tar}'", check=False)
    if pre_error is not None:
        ui.progress_done("预备份目标设备现有短信数据库 + app_parts", False,
                         str(pre_error))
        ui.show_warning(
            "目标设备短信预备份失败！继续恢复可能无法回滚。"
            "建议取消并检查设备空间、tar 命令和连接状态。"
        )
        chosen = ui.select_items(
            [{"id": "cancel", "label": "取消短信恢复（推荐）"},
             {"id": "continue", "label": "忽略预备份失败，仍继续恢复（风险自负）"}],
            title="预备份失败，是否继续？",
            default_all=False,
        )
        if "continue" not in chosen or "cancel" in chosen:
            raise RuntimeError("用户在预备份失败后取消短信恢复")
    elif any_pre_ok:
        ui.progress_done("预备份目标设备现有短信数据库 + app_parts", True,
                         "已保存到 sms/_pre_restore_backup_*_<sub>.tar  "
                         "（如需回滚请手动恢复这些文件）")
    else:
        ui.progress_done("预备份目标设备现有短信数据库 + app_parts", True,
                         "目标暂无 databases/app_parts（新机），无需备份")

    # 阶段 C & D：对每个包含的子目录做 推送解包 + 权限修复
    # 注意只恢复 manifest.included 中 True 的那些
    restored_count = 0
    failures: List[str] = []
    allowed_tar_by_subdir = {name: tar for name, tar, _ in SUB_DIRS}
    for meta in sub_dirs_meta:
        sub_dir = meta.get("sub_dir")
        tar_name = meta.get("tar_file")
        if not meta.get("included") or not tar_name:
            continue
        if sub_dir not in allowed_tar_by_subdir or tar_name != allowed_tar_by_subdir[sub_dir]:
            failures.append(f"manifest 含非法条目: {sub_dir!r}/{tar_name!r}")
            continue
        local_tar = sms_root / tar_name
        if not local_tar.exists():
            logger.warning("manifest 标记包含 %s，但本地 tar 不存在，跳过",
                           sub_dir)
            failures.append(f"{sub_dir}: 本地 tar 不存在")
            continue
        # 推送解包
        step_push = f"推送并解包 {sub_dir}/"
        ui.progress_start(step_push)
        tmp_remote = f"/data/local/tmp/_restore_sms_{sub_dir}_{ts}.tar"
        remote_backup: Optional[str] = None
        try:
            remote_backup = _push_untar_remote(
                local_tar, tmp_remote, PKG_ROOT, sub_dir
            )
        except Exception as exc:  # noqa: BLE001
            adb.shell(f"rm -f '{tmp_remote}'", check=False)
            ui.progress_done(step_push, False, str(exc))
            failures.append(f"{sub_dir}: {exc}")
            continue
        size_str = _human_size(local_tar.stat().st_size)
        ui.progress_done(step_push, True, size_str)

        # 权限修复
        step_fix = f"修复 {sub_dir}/ owner/permission/SELinux"
        ui.progress_start(step_fix)
        try:
            ug, z_info = _fix_owner_selinux(PKG_ROOT, sub_dir)
        except Exception as exc:  # noqa: BLE001
            ui.progress_done(step_fix, False, str(exc))
            try:
                _rollback_remote_dir(PKG_ROOT, sub_dir, remote_backup)
            except Exception:  # noqa: BLE001
                logger.exception("权限修复失败后自动回滚 %s 也失败", sub_dir)
            failures.append(f"{sub_dir}: {exc}")
            continue
        detail = f"owner={ug}"
        if z_info:
            detail += f"  context='{z_info.strip()[-80:]}'"
        ui.progress_done(step_fix, True, detail)
        _discard_remote_backup(remote_backup)
        restored_count += 1

    # 阶段 F：重启
    ui.progress_start("重启系统 Provider（让数据库生效）")
    _stop_all_sms_processes()
    ui.progress_done("重启系统 Provider（让数据库生效）", True,
                     "已 force-stop；下次打开「短信」App 会自动加载")

    if restored_count == 0:
        raise RuntimeError("sms 恢复中没有任何子目录被成功解包，请查看以上进度错误")
    if failures:
        raise RuntimeError("短信恢复部分失败: " + "; ".join(failures))

    ui.show_warning(
        "短信/彩信恢复已完成。如「短信」App 出现闪退或会话错乱，请先尝试"
        "「设置 → 应用 → 短信 → 存储 → 清空数据」后重试；"
        f"若仍不可用，请手动使用 {sms_root}/_pre_restore_backup_<ts>_*.tar "
        "预备份文件进行回滚。"
    )
