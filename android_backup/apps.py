"""第三方应用与应用数据 备份/恢复实现。

备份：
- `pm list packages -3` 列出第三方应用
- 让用户在 PyWebIO 二级页面勾选要备份的包
- 对每个包：
  - 拉取所有 APK（含 split）：`pm path <pkg>` -> `adb pull`
  - root tar 打包 `/data/data/<pkg>` 为 `data.tar`
  - 若存在则打包 `/sdcard/Android/data/<pkg>` 为 `ext_data.tar`
  - 若存在则打包 `/sdcard/Android/obb/<pkg>` 为 `obb.tar`
  - 写 `info.json`
- 在备份根目录写 `apps/manifest.json` 聚合包元信息

恢复：
- 读取 `apps/manifest.json`，二级勾选要恢复的包
- 对每个包：
  1) 若设备未装 + 备份含 APK，则 `pm install`（多 APK 用 install-create/-write/-commit）
  2) `am force-stop <pkg>`
  3) tar 解包覆盖 `/data/data/<pkg>`，`chown -R <uid>:<uid>`、`restorecon -R`
  4) 按需解包 `ext_data.tar` / `obb.tar`
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import adb, ui

logger = logging.getLogger("android_backup.apps")

ITEM = {
    "id": "apps",
    "label": "应用与应用数据",
    "files": ["apps/manifest.json", "apps/<pkg>/..."],
}

DATA_DIR_FMT = "/data/user/0/{pkg}"
EXT_DATA_DIR_FMT = "/sdcard/Android/data/{pkg}"
OBB_DIR_FMT = "/sdcard/Android/obb/{pkg}"

# 备份 /data/user/0/<pkg> 时排除的子目录（cache 类不必备份且会持续变化）
DATA_TAR_EXCLUDES = ["cache", "code_cache", "no_backup"]


# -------------------------- 辅助函数 --------------------------

def _remote_exists(path: str) -> bool:
    proc = adb.run_adb(
        ["shell", f"[ -e {path} ] && echo YES || echo NO"], check=False
    )
    return proc.returncode == 0 and "YES" in proc.stdout


def list_third_party_packages() -> List[str]:
    """通过 `pm list packages -3` 返回第三方应用包名列表。"""
    out = adb.shell("pm list packages -3")
    pkgs: List[str] = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            pkgs.append(line[len("package:"):])
    pkgs.sort()
    return pkgs


def _pm_paths(pkg: str) -> List[str]:
    """`pm path <pkg>` -> 设备上的 apk 路径列表。"""
    try:
        out = adb.shell(f"pm path {pkg}")
    except Exception:
        return []
    paths: List[str] = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            paths.append(line[len("package:"):])
    return paths


def _is_installed(pkg: str) -> bool:
    return bool(_pm_paths(pkg))


def _get_app_info(pkg: str) -> Dict:
    """通过 dumpsys package 提取 versionName/versionCode/userId 与用户可见 label。"""
    info: Dict = {"pkg": pkg, "version_name": None, "version_code": None,
                  "uid": None, "label": None}
    try:
        out = adb.shell(f"dumpsys package {pkg}")
    except Exception:
        return info
    m = re.search(r"versionName=(\S+)", out)
    if m:
        info["version_name"] = m.group(1)
    m = re.search(r"versionCode=(\d+)", out)
    if m:
        info["version_code"] = int(m.group(1))
    # 不同 Android 版本的字段名差异：userId / appId 都可能出现
    for key in ("userId", "appId"):
        m = re.search(rf"\b{key}=(\d+)", out)
        if m:
            info["uid"] = int(m.group(1))
            break
    # 兜底：直接 stat /data/user/0/<pkg> 取 owner uid（要求设备已安装该包）
    if not info["uid"]:
        info["uid"] = _stat_pkg_uid(pkg)
    # 用户可见 label
    info["label"] = _get_app_label(pkg)
    return info


def _get_app_label(pkg: str) -> Optional[str]:
    """获取应用的用户可见名称。

    通过 `aapt2/aapt dump badging base.apk` 解析；失败时返回 None。
    """
    paths = _pm_paths(pkg)
    base_apk = None
    for p in paths:
        if p.endswith("base.apk"):
            base_apk = p
            break
    if not base_apk and paths:
        base_apk = paths[0]
    if not base_apk:
        return None
    proc = adb.run_adb(
        ["shell", f"aapt2 dump badging {base_apk} 2>/dev/null"], check=False
    )
    if proc.returncode != 0 or not proc.stdout:
        # 一些设备没有 aapt2，尝试 aapt
        proc = adb.run_adb(
            ["shell", f"aapt dump badging {base_apk} 2>/dev/null"], check=False
        )
    if proc.returncode != 0 or not proc.stdout:
        return None
    # 优先 zh / 默认 application-label
    m_zh = re.search(r"application-label-zh[^:]*:'([^']*)'", proc.stdout)
    if m_zh:
        return m_zh.group(1)
    m = re.search(r"application-label:'([^']*)'", proc.stdout)
    if m:
        return m.group(1)
    return None


def _stat_pkg_uid(pkg: str) -> Optional[int]:
    """通过 `stat -c %u /data/user/0/<pkg>` 直接读取应用数据目录 owner uid。"""
    proc = adb.run_adb(
        ["shell", f"stat -c %u /data/user/0/{pkg}"], check=False
    )
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def _tar_remote_dir(remote_dir: str, tmp_remote_tar: str,
                    excludes: Optional[List[str]] = None,
                    force_stop_pkg: Optional[str] = None) -> bool:
    """在设备上 tar 打包某目录。返回是否成功且非空。

    - 自动解析 symlink，避免 cd 到 symlink 后 tar 行为异常
    - 可选 force_stop_pkg：打包前停止应用，降低 "file changed as we read it"
    - 可选 excludes：排除 cache 类子目录
    - tar 命令使用 `--warning=no-file-changed`/退出码 1 容忍模式：
      toybox tar 没有该参数，这里采用 `|| [ $? -eq 1 ]` 做容忍
    """
    if not _remote_exists(remote_dir):
        return False

    # 1) 解析 symlink (例如 /data/data/<pkg> -> /data/user/0/<pkg>)
    real = adb.run_adb(["shell", f"readlink -f {remote_dir}"], check=False)
    real_path = real.stdout.strip() if real.returncode == 0 else remote_dir
    if not real_path:
        real_path = remote_dir
    if real_path != remote_dir:
        logger.info("解析符号链接: %s -> %s", remote_dir, real_path)

    # 2) 备份前 force-stop，降低运行中文件被改写的概率
    if force_stop_pkg:
        adb.shell(f"am force-stop {force_stop_pkg}", check=False)

    parent = real_path.rsplit("/", 1)[0] or "/"
    base = real_path.rsplit("/", 1)[1]
    excl_args = ""
    if excludes:
        excl_args = " " + " ".join(f"--exclude={base}/{e}" for e in excludes)
    # toybox tar 在 "file changed" 时返回 1；我们容忍 1，但仍要求生成的 tar 存在
    cmd = (
        f"cd {parent} && (tar -cpf {tmp_remote_tar}{excl_args} {base}; rc=$?; "
        f"if [ $rc -ne 0 ] && [ $rc -ne 1 ]; then exit $rc; fi)"
    )
    proc = adb.run_adb(["shell", cmd], check=False)
    if proc.returncode != 0:
        logger.warning("tar 打包失败 %s: %s", real_path, proc.stderr.strip())
        return False
    adb.shell(f"chmod 644 {tmp_remote_tar}", check=False)
    if not _remote_exists(tmp_remote_tar):
        return False
    # 检查 tar 大小
    size_proc = adb.run_adb(["shell", f"stat -c %s {tmp_remote_tar}"], check=False)
    try:
        size = int(size_proc.stdout.strip())
    except ValueError:
        size = -1
    if size <= 0:
        logger.warning("tar 文件为空: %s", tmp_remote_tar)
        return False
    return True


def _pull_to(local: Path, tmp_remote: str) -> None:
    local.parent.mkdir(parents=True, exist_ok=True)
    try:
        adb.pull(tmp_remote, local)
    finally:
        adb.shell(f"rm -f {tmp_remote}", check=False)


# -------------------------- 备份 --------------------------

def select_packages_interactive(packages: List[str]) -> List[str]:
    """二级勾选页面：从已枚举的包列表中选择要备份的包。

    会调用 aapt2 解析每个 base.apk 的 application-label 以提供用户可见名称；
    若解析失败则退回到包名本身。默认全部勾选。
    """
    if not packages:
        return []
    logger.info("正在解析 %d 个应用的可见名称…", len(packages))
    items = []
    for pkg in packages:
        label = _get_app_label(pkg)
        display = f"{label} ({pkg})" if label else pkg
        items.append({"id": pkg, "label": display})
    return ui.select_items(items, title="选择要备份的应用（可多选，默认全选）",
                           default_all=True)


def backup_one_package(pkg: str, dest_dir: Path) -> Dict:
    """备份单个包，返回该包在 manifest 中的元信息项。"""
    pkg_dir = dest_dir / "apps" / pkg
    pkg_dir.mkdir(parents=True, exist_ok=True)
    apk_dir = pkg_dir / "apk"

    info = _get_app_info(pkg)
    has_apk = False
    apk_files: List[str] = []
    has_data = False
    has_ext = False
    has_obb = False

    # 1) APK
    paths = _pm_paths(pkg)
    if paths:
        apk_dir.mkdir(parents=True, exist_ok=True)
        for remote_apk in paths:
            local_apk = apk_dir / Path(remote_apk).name
            try:
                adb.pull(remote_apk, local_apk)
                apk_files.append(local_apk.name)
                has_apk = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("拉取 APK 失败 %s: %s", remote_apk, exc)
    else:
        logger.warning("未找到 APK 路径: %s", pkg)

    token = _dt.datetime.now().strftime("%H%M%S%f")

    # 2) /data/user/0/<pkg> （/data/data/<pkg> 是其 symlink）
    data_dir = DATA_DIR_FMT.format(pkg=pkg)
    tmp_tar = f"/data/local/tmp/app_backup_{pkg}_data_{token}.tar"
    if _tar_remote_dir(data_dir, tmp_tar,
                       excludes=DATA_TAR_EXCLUDES,
                       force_stop_pkg=pkg):
        _pull_to(pkg_dir / "data.tar", tmp_tar)
        has_data = True
    else:
        logger.warning("数据目录 %s 不存在或打包失败，跳过", data_dir)

    # 3) /sdcard/Android/data/<pkg>
    ext_dir = EXT_DATA_DIR_FMT.format(pkg=pkg)
    tmp_tar = f"/sdcard/app_backup_{pkg}_ext_{token}.tar"
    if _tar_remote_dir(ext_dir, tmp_tar):
        _pull_to(pkg_dir / "ext_data.tar", tmp_tar)
        has_ext = True

    # 4) /sdcard/Android/obb/<pkg>
    obb_dir = OBB_DIR_FMT.format(pkg=pkg)
    tmp_tar = f"/sdcard/app_backup_{pkg}_obb_{token}.tar"
    if _tar_remote_dir(obb_dir, tmp_tar):
        _pull_to(pkg_dir / "obb.tar", tmp_tar)
        has_obb = True

    # 5) info.json
    entry = {
        "pkg": pkg,
        "label": info.get("label"),
        "version_name": info.get("version_name"),
        "version_code": info.get("version_code"),
        "uid": info.get("uid"),
        "has_apk": has_apk,
        "apk_files": apk_files,
        "has_data": has_data,
        "has_ext_data": has_ext,
        "has_obb": has_obb,
    }
    (pkg_dir / "info.json").write_text(
        json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("应用备份完成: %s -> %s", pkg, pkg_dir)
    return entry


def backup(dest_dir: Path) -> None:
    pkgs = list_third_party_packages()
    if not pkgs:
        logger.warning("未在设备上找到第三方应用")
        return
    logger.info("第三方应用共 %d 个", len(pkgs))
    selected = select_packages_interactive(pkgs)
    if not selected:
        logger.info("用户未选择任何应用，跳过 apps 备份")
        return

    apps_root = dest_dir / "apps"
    apps_root.mkdir(parents=True, exist_ok=True)
    entries: List[Dict] = []
    for pkg in selected:
        label = _get_app_label(pkg)
        display = f"{label} ({pkg})" if label else pkg
        ui.progress_start(f"备份应用 {display}")
        try:
            entry = backup_one_package(pkg, dest_dir)
            entries.append(entry)
            parts = []
            if entry.get("has_apk"):
                parts.append("APK✓")
            if entry.get("has_data"):
                parts.append("data✓")
            if entry.get("has_ext_data"):
                parts.append("ext✓")
            if entry.get("has_obb"):
                parts.append("obb✓")
            detail = " ".join(parts) or "(无内容)"
            ui.progress_done(f"备份应用 {display}", True, detail)
        except Exception as exc:  # noqa: BLE001
            logger.exception("备份应用失败 %s", pkg)
            entries.append({"pkg": pkg, "error": str(exc)})
            ui.progress_done(f"备份应用 {display}", False, str(exc))

    (apps_root / "manifest.json").write_text(
        json.dumps({"created_at": _dt.datetime.now().isoformat(timespec="seconds"),
                    "packages": entries},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("apps 备份完成，共 %d 个包", len(entries))


# -------------------------- 恢复 --------------------------

def _install_apks(apk_local_paths: List[Path]) -> None:
    """安装 APK：单 APK 直接 `pm install -r`，多 APK 走 install session。

    注意：APK 必须 push 到 `/data/local/tmp/`，不能放 `/sdcard/`：后者是
    fuse 文件系统，`system_server` 没有读权限，会触发 SELinux 拒绝。
    """
    if len(apk_local_paths) == 1:
        local = apk_local_paths[0]
        remote = f"/data/local/tmp/_install_{local.name}"
        adb.push(local, remote)
        adb.shell(f"chmod 644 {remote}", check=False)
        try:
            out = adb.shell(f"pm install -r {remote}")
            if "Success" not in out:
                raise RuntimeError(f"pm install 失败: {out.strip()}")
        finally:
            adb.shell(f"rm -f {remote}", check=False)
        return

    # 多 APK：install session
    sizes = [p.stat().st_size for p in apk_local_paths]
    total = sum(sizes)
    out = adb.shell(f"pm install-create -r -S {total}")
    m = re.search(r"\[(\d+)\]", out)
    if not m:
        raise RuntimeError(f"pm install-create 失败: {out.strip()}")
    session = m.group(1)
    try:
        for idx, local in enumerate(apk_local_paths):
            remote = f"/data/local/tmp/_install_{idx}_{local.name}"
            adb.push(local, remote)
            adb.shell(f"chmod 644 {remote}", check=False)
            try:
                w = adb.shell(
                    f"pm install-write -S {local.stat().st_size} {session} {idx} {remote}"
                )
                if "Success" not in w:
                    raise RuntimeError(f"install-write 失败: {w.strip()}")
            finally:
                adb.shell(f"rm -f {remote}", check=False)
        commit = adb.shell(f"pm install-commit {session}")
        if "Success" not in commit:
            raise RuntimeError(f"install-commit 失败: {commit.strip()}")
    except Exception:
        adb.shell(f"pm install-abandon {session}", check=False)
        raise


def _untar_to(local_tar: Path, target_parent: str, fix_owner: Optional[Tuple[int, int]],
              restorecon_path: Optional[str]) -> None:
    """push tar 后在 target_parent 解包。

    注意：tar 必须落在 `/data/local/tmp/`，避免 fuse 文件系统的 SELinux 限制；
    `chown` 与 `restorecon` 必须作用在真实路径（如 `/data/user/0/<pkg>`），
    不能走 symlink，否则只 relabel 链接本身。
    """
    tmp_remote = f"/data/local/tmp/_restore_{local_tar.name}"
    adb.push(local_tar, tmp_remote)
    try:
        adb.shell(f"cd {target_parent} && tar -xpf {tmp_remote}")
        if fix_owner and restorecon_path:
            uid, gid = fix_owner
            # 顶层目录权限固定为 0700，与 PackageManager 创建时一致
            adb.shell(f"chmod 0700 {restorecon_path}", check=False)
            # 用 find -exec 逐个 chown：toybox `chown -R` 在遇到某些
            # 符号链接 / 特殊文件时可能中断，导致深层文件保留旧 uid
            adb.shell(
                f"find {restorecon_path} -exec chown {uid}:{gid} {{}} +",
                check=False,
            )
            # 双跑 restorecon -RF，强制按 file_contexts + seapp_contexts
            # 重打 SELinux 标签（含 MLS 类别）
            adb.shell(f"restorecon -RF {restorecon_path}", check=False)
            adb.shell(f"restorecon -RF {restorecon_path}", check=False)
            # 打印一行供排查；预期 owner=uXX_aYYY，context 含 c<categories>
            proc = adb.run_adb(
                ["shell", f"ls -ldZ {restorecon_path}"], check=False
            )
            if proc.returncode == 0:
                logger.info("恢复后顶层属性: %s", proc.stdout.strip())
    finally:
        adb.shell(f"rm -f {tmp_remote}", check=False)


def restore_one_package(entry: Dict, src_dir: Path) -> Tuple[bool, str]:
    """恢复单个包。返回 (成功?, 备注)。"""
    pkg = entry["pkg"]
    pkg_dir = src_dir / "apps" / pkg
    if not pkg_dir.is_dir():
        return False, f"目录不存在: {pkg_dir}"

    # 1) APK 安装
    installed = _is_installed(pkg)
    if not installed:
        if entry.get("has_apk"):
            apk_files = sorted((pkg_dir / "apk").glob("*.apk"))
            if not apk_files:
                return False, "info.json 标记 has_apk 但目录中无 apk 文件"
            try:
                _install_apks(apk_files)
                logger.info("已安装 %s", pkg)
            except Exception as exc:  # noqa: BLE001
                return False, f"安装失败: {exc}"
        else:
            return False, "设备未安装该应用，且备份不含 APK，需手动安装"

    # 2) 停应用
    adb.shell(f"am force-stop {pkg}", check=False)

    # 3) 取目标 uid（设备上当前 uid，而不是备份时的）
    info = _get_app_info(pkg)
    uid = info.get("uid")
    if not uid:
        # 再兜底一次：直接 stat
        uid = _stat_pkg_uid(pkg)
    if not uid:
        return False, (
            "无法获取目标 uid（dumpsys package 没匹配到 userId/appId，"
            f"且 /data/user/0/{pkg} 也不可访问），跳过数据恢复"
        )

    # 4) /data/user/0/<pkg>  (真实路径；/data/data/<pkg> 是其 symlink)
    data_tar = pkg_dir / "data.tar"
    if data_tar.exists():
        data_dir = DATA_DIR_FMT.format(pkg=pkg)
        try:
            # 让 PackageManager 自己重建一个干净的、上下文/权限/uid 都正确的
            # /data/user/0/<pkg> 目录，再把备份内容解进去覆盖
            adb.shell(f"pm clear {pkg}", check=False)
            adb.shell(f"am force-stop {pkg}", check=False)
            _untar_to(
                data_tar,
                target_parent="/data/user/0",
                fix_owner=(uid, uid),
                restorecon_path=data_dir,
            )
            logger.info("已恢复 %s 数据目录 (uid=%d)", pkg, uid)
        except Exception as exc:  # noqa: BLE001
            return False, f"data.tar 解包失败: {exc}"

    # 5) ext_data.tar
    ext_tar = pkg_dir / "ext_data.tar"
    if ext_tar.exists():
        ext_dir = EXT_DATA_DIR_FMT.format(pkg=pkg)
        try:
            adb.shell(f"rm -rf {ext_dir}", check=False)
            adb.shell("mkdir -p /sdcard/Android/data", check=False)
            _untar_to(ext_tar, target_parent="/sdcard/Android/data",
                      fix_owner=None, restorecon_path=None)
            logger.info("已恢复 %s 外部存储数据", pkg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ext_data.tar 解包失败: %s", exc)

    # 6) obb.tar
    obb_tar = pkg_dir / "obb.tar"
    if obb_tar.exists():
        obb_dir = OBB_DIR_FMT.format(pkg=pkg)
        try:
            adb.shell(f"rm -rf {obb_dir}", check=False)
            adb.shell("mkdir -p /sdcard/Android/obb", check=False)
            _untar_to(obb_tar, target_parent="/sdcard/Android/obb",
                      fix_owner=None, restorecon_path=None)
            logger.info("已恢复 %s obb 数据", pkg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("obb.tar 解包失败: %s", exc)

    return True, "完成"


def restore(src_dir: Path) -> None:
    apps_manifest = src_dir / "apps" / "manifest.json"
    if not apps_manifest.exists():
        raise FileNotFoundError(f"未找到 {apps_manifest}")
    data = json.loads(apps_manifest.read_text(encoding="utf-8"))
    packages: List[Dict] = [p for p in data.get("packages", []) if "pkg" in p]
    if not packages:
        logger.warning("apps/manifest.json 中无可恢复的包")
        return

    items = []
    for p in packages:
        label = p.get("label")
        ver = p.get("version_name")
        if label:
            display = f"{label} ({p['pkg']}, v{ver})"
        else:
            display = f"{p['pkg']} (v{ver})"
        items.append({"id": p["pkg"], "label": display})
    selected = ui.select_items(items, title="选择要恢复的应用（可多选，默认全选）",
                               default_all=True)
    if not selected:
        logger.info("用户未选择任何应用，跳过 apps 恢复")
        return

    by_pkg = {p["pkg"]: p for p in packages}
    success, failed = 0, 0
    for pkg in selected:
        entry = by_pkg[pkg]
        label = entry.get("label")
        ver = entry.get("version_name")
        if label:
            display = f"{label} ({pkg}, v{ver})"
        else:
            display = f"{pkg} (v{ver})"
        ui.progress_start(f"恢复应用 {display}")
        try:
            ok, msg = restore_one_package(entry, src_dir)
        except Exception as exc:  # noqa: BLE001
            logger.exception("恢复应用失败 %s", pkg)
            ok, msg = False, str(exc)
        if ok:
            success += 1
            logger.info("✅ %s: %s", pkg, msg)
            ui.progress_done(f"恢复应用 {display}", True, msg)
        else:
            failed += 1
            logger.error("❌ %s: %s", pkg, msg)
            ui.progress_done(f"恢复应用 {display}", False, msg)

    logger.info("apps 恢复完成: 成功 %d, 失败 %d", success, failed)
