"""相册（照片 + 视频）备份/恢复实现。

备份策略：
1. 在 /sdcard 下枚举候选目录（find -maxdepth 4），筛选出"至少包含一个图片/视频
   文件"且不在排除清单（Android/、.thumbnails、Music/ 等）中的目录作为相册。
2. 让用户二级勾选要备份的相册目录。
3. 对每个相册，逐文件 adb pull（保留时间戳，自动降级）到本地
   backup_dir/photos/<rel>/<subpath>。每个文件完成后立即在网页输出一条进度。
4. 单个文件拉取失败时自动重试最多 3 次，仍失败则跳过并记录。
5. 写 photos/manifest.json 聚合每个相册的 file_count / size_bytes。

恢复策略：
1. 读 photos/manifest.json → 二级勾选相册。
2. 对每个相册，逐文件 adb push 回 /sdcard/<rel>/<subpath>。
   每个文件完成后立即在网页输出一条进度；失败重试 3 次后跳过。
3. 尝试发 MEDIA_SCANNER_SCAN_FILE 广播让系统相册刷新（失败静默）。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import posixpath
import shlex
import time
from pathlib import Path, PurePosixPath
from typing import Dict, List, Tuple

from . import adb, ui

logger = logging.getLogger("android_backup.photos")

ITEM = {
    "id": "photos",
    "label": "相册（照片与视频）",
    "files": ["photos/manifest.json", "photos/DCIM/*", "photos/Pictures/*"],
}

# 媒体扩展名白名单（小写比对）
_IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".heic", ".heif", ".tif", ".tiff", ".raw", ".dng",
    ".cr2", ".nef", ".arw",
}
_VIDEO_EXTS = {
    ".mp4", ".mov", ".3gp", ".mkv", ".avi", ".webm",
    ".m4v", ".wmv", ".flv",
}
MEDIA_EXTS = _IMAGE_EXTS | _VIDEO_EXTS

# 候选目录扫描时遇到即跳过的目录名（大小写敏感，/sdcard 下通常都是小写或正常大小写）
EXCLUDED_DIR_NAMES = {
    "Android", "data", "obb",
    "Music", "Ringtones", "Alarms", "Notifications",
    "Podcasts", "Recordings", "Audiobooks",
    ".thumbnails", ".cache",
}

# 远端根目录：优先用 /sdcard，失败时回退到 /storage/emulated/0
CANDIDATE_ROOTS = ["/sdcard", "/storage/emulated/0"]


# -------------------------- 辅助函数 --------------------------

def _is_media_file(filename: str) -> bool:
    return Path(filename).suffix.lower() in MEDIA_EXTS


def _human_size(n: int) -> str:
    step = 1024
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < step:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} PB"


def _resolve_root() -> str:
    """返回可用的外部存储根路径（/sdcard 或 /storage/emulated/0）。"""
    for root in CANDIDATE_ROOTS:
        proc = adb.run_adb(
            ["shell", f"[ -d {root} ] && echo YES || echo NO"], check=False
        )
        if proc.returncode == 0 and "YES" in proc.stdout:
            return root
    raise RuntimeError(
        f"无法在设备上找到外部存储根路径（尝试了 {CANDIDATE_ROOTS}），"
        f"请确认设备已解锁并允许 adb 访问 /sdcard"
    )


def _remote_path_exists(path: str) -> bool:
    proc = adb.run_adb(
        ["shell", f"[ -e {shlex.quote(path)} ] && echo YES || echo NO"],
        check=False,
    )
    return proc.returncode == 0 and "YES" in proc.stdout


def _list_dirs_maxdepth(root: str, maxdepth: int = 4,
                        timeout_sec: int = 60) -> List[str]:
    """返回 root 下最大深度 maxdepth 的所有子目录（不含 root 本身）。

    用 `find ... -type d -maxdepth N`，带 timeout 保护。
    """
    import subprocess
    cmd = [adb._adb_binary(), "shell",
           f"find {root} -type d -maxdepth {maxdepth}"]
    logger.info("$ %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, text=True, capture_output=True, timeout=timeout_sec, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning("find 目录扫描超时 %ds：%s", timeout_sec, exc)
        return []
    if proc.stdout:
        for line in proc.stdout.rstrip().splitlines():
            logger.info("  out| %s", line[:120])
    if proc.stderr:
        for line in proc.stderr.rstrip().splitlines():
            logger.info("  err| %s", line)
    if proc.returncode != 0:
        logger.warning("find 目录扫描失败 (exit=%d): %s",
                       proc.returncode, proc.stderr.strip())
        return []
    # 过滤：非空、非 root 自身
    return [
        line.strip()
        for line in proc.stdout.splitlines()
        if line.strip() and line.strip() != root
    ]


def _dir_has_media_files(remote_dir: str) -> bool:
    """判断远端目录下（仅一层）是否至少有一个媒体扩展名的普通文件。"""
    proc = adb.run_adb(
        ["shell", f"ls -p {shlex.quote(remote_dir)} 2>/dev/null"], check=False
    )
    if proc.returncode != 0:
        return False
    for line in proc.stdout.splitlines():
        name = line.strip()
        if not name or name.endswith("/"):
            continue
        if _is_media_file(name):
            return True
    return False


def _scan_album_dirs(root: str) -> List[Dict]:
    """扫描 root 下所有可能的相册目录，返回条目列表。

    每个条目字段：
      - rel_path: 相对 root 的路径（如 "DCIM/Camera"）
      - remote: 绝对远端路径
    """
    candidates: List[Dict] = []
    all_dirs = _list_dirs_maxdepth(root, maxdepth=4)
    if not all_dirs:
        # 扫描失败：退化为"直接检查几个知名目录是否存在"的保守策略
        fallback = [
            f"{root}/DCIM/Camera",
            f"{root}/DCIM/Screenshots",
            f"{root}/Pictures/Screenshots",
            f"{root}/Pictures/WeiXin",
            f"{root}/Pictures",
            f"{root}/DCIM",
        ]
        for remote in fallback:
            if _remote_path_exists(remote) and _dir_has_media_files(remote):
                candidates.append({
                    "rel_path": posixpath.relpath(remote, root),
                    "remote": remote,
                })
        return candidates

    # 正常路径：先按 排除清单 + 隐藏目录前缀 过滤
    kept: List[str] = []
    for remote in all_dirs:
        rel = posixpath.relpath(remote, root)
        parts = rel.split("/")
        # 任何一段是排除名或隐藏前缀 → 丢弃
        bad = False
        for p in parts:
            if p.startswith("."):
                bad = True
                break
            if p in EXCLUDED_DIR_NAMES:
                bad = True
                break
        if bad:
            continue
        kept.append(remote)

    # 对每个目录检查是否包含媒体文件
    for remote in kept:
        if _dir_has_media_files(remote):
            candidates.append({
                "rel_path": posixpath.relpath(remote, root),
                "remote": remote,
            })

    # 父子目录去重：如果 A 是 B 的祖先且都是候选，仅保留祖先（范围更大）
    removed = set()
    sorted_rels = sorted(c["rel_path"] for c in candidates)
    for i, r in enumerate(sorted_rels):
        for r2 in sorted_rels[i + 1:]:
            if r2.startswith(r + "/"):
                removed.add(r2)
    deduped = [c for c in candidates if c["rel_path"] not in removed]
    # 按 rel_path 排序保证 UI 稳定
    deduped.sort(key=lambda c: c["rel_path"])
    return deduped


def _estimate_dir_stats(remote_dir: str) -> Dict:
    r"""预估远端目录的文件数和总字节（仅统计扩展名匹配的媒体文件）。

    原本想用 `find <dir> -type f \( -name "*.jpg" -o ... \) -exec stat ...` 这类
    组合命令，但代价过高；这里退化为：`find -type f | wc -l` + `du -sk` 的简化。
    如果命令失败返回 {files=0, size=0}。
    """
    files_proc = adb.run_adb(
        ["shell", f"find {shlex.quote(remote_dir)} -type f | wc -l"], check=False
    )
    try:
        files = int(files_proc.stdout.strip() or "0")
    except ValueError:
        files = 0
    du_proc = adb.run_adb(
        ["shell", f"du -sk {shlex.quote(remote_dir)} 2>/dev/null | awk '{{print $1}}'"],
        check=False,
    )
    try:
        kb = int(du_proc.stdout.strip() or "0")
        size = kb * 1024
    except ValueError:
        size = 0
    return {"files": files, "size": size}


def _count_files_and_size(local_dir: Path) -> Tuple[int, int]:
    """统计本地目录下的文件数和总字节。"""
    files, size = 0, 0
    if not local_dir.exists():
        return 0, 0
    for p in local_dir.rglob("*"):
        if p.is_file():
            files += 1
            try:
                size += p.stat().st_size
            except OSError:
                pass
    return files, size


# -------------------------- 逐文件拉取/推送（带重试） --------------------------

MAX_RETRIES = 3
RETRY_DELAY = 0.5  # 重试间隔（秒）
# 文件大小超过此阈值时，额外输出 progress_start 行提示"正在传输大文件"
LARGE_FILE_THRESHOLD = 10 * 1024 * 1024  # 10 MB


def _list_remote_media_files(remote_dir: str) -> List[str]:
    """递归列出远端目录下所有媒体文件的绝对路径。"""
    proc = adb.run_adb(
        ["shell", f"find {shlex.quote(remote_dir)} -type f 2>/dev/null"],
        check=False,
    )
    if proc.returncode != 0 and not proc.stdout:
        return []
    result: List[str] = []
    for line in proc.stdout.splitlines():
        p = line.strip()
        if p and _is_media_file(p):
            result.append(p)
    return result


def _remote_file_size(remote_path: str) -> int:
    """获取远端文件大小（字节）；失败返回 0。"""
    proc = adb.run_adb(
        ["shell", f"stat -c %s {shlex.quote(remote_path)} 2>/dev/null"],
        check=False,
    )
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return 0


def _pull_single_file(remote_file: str, local_file: Path) -> None:
    """拉取单个文件，带 -a 降级。失败抛 AdbError。"""
    local_file.parent.mkdir(parents=True, exist_ok=True)
    proc = adb.run_adb(
        ["pull", "-a", remote_file, str(local_file)], check=False, capture=True,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "") + (proc.stdout or "")
        if "unknown option" in stderr.lower() or "invalid option" in stderr.lower():
            adb.run_adb(["pull", remote_file, str(local_file)], check=True)
        else:
            raise adb.AdbError(
                f"adb pull 失败: {remote_file} (exit={proc.returncode}) "
                f"{proc.stderr.strip()}"
            )
    if not local_file.exists():
        raise adb.AdbError(f"adb pull 完成但本地文件不存在: {local_file}")


def _pull_file_with_retry(remote_file: str, local_file: Path,
                           max_retries: int = MAX_RETRIES) -> bool:
    """带重试的单文件拉取。成功 True，全部失败 False。"""
    for attempt in range(1, max_retries + 1):
        try:
            _pull_single_file(remote_file, local_file)
            return True
        except Exception as exc:  # noqa: BLE001
            if attempt < max_retries:
                logger.warning("拉取 %s 第 %d/%d 次失败: %s，%.1fs 后重试",
                               posixpath.basename(remote_file),
                               attempt, max_retries, exc, RETRY_DELAY)
                time.sleep(RETRY_DELAY)
            else:
                logger.error("拉取 %s %d 次均失败，跳过",
                             posixpath.basename(remote_file), max_retries)
                return False
    return False


def _list_local_media_files(local_dir: Path) -> List[Path]:
    """递归列出本地目录下所有媒体文件路径（已排序）。"""
    if not local_dir.exists():
        return []
    return sorted(
        p for p in local_dir.rglob("*")
        if p.is_file() and _is_media_file(p.name)
    )


def _push_single_file(local_file: Path, remote_file: str) -> None:
    """推送单个文件到远端。失败抛 AdbError。"""
    if not local_file.exists():
        raise FileNotFoundError(f"本地文件不存在: {local_file}")
    remote_parent = posixpath.dirname(remote_file)
    if remote_parent:
        adb.shell(f"mkdir -p {shlex.quote(remote_parent)}", check=False)
    adb.run_adb(["push", str(local_file), remote_file], check=True)


def _push_file_with_retry(local_file: Path, remote_file: str,
                           max_retries: int = MAX_RETRIES) -> bool:
    """带重试的单文件推送。成功 True，全部失败 False。"""
    for attempt in range(1, max_retries + 1):
        try:
            _push_single_file(local_file, remote_file)
            return True
        except Exception as exc:  # noqa: BLE001
            if attempt < max_retries:
                logger.warning("推送 %s 第 %d/%d 次失败: %s，%.1fs 后重试",
                               local_file.name, attempt, max_retries,
                               exc, RETRY_DELAY)
                time.sleep(RETRY_DELAY)
            else:
                logger.error("推送 %s %d 次均失败，跳过",
                             local_file.name, max_retries)
                return False
    return False


# -------------------------- 交互 --------------------------

def select_albums_interactive(albums: List[Dict]) -> List[str]:
    """二级勾选相册。返回选中的 rel_path 列表。"""
    if not albums:
        return []
    items = []
    for a in albums:
        rel = a["rel_path"]
        try:
            stats = _estimate_dir_stats(a["remote"])
        except Exception:  # noqa: BLE001
            stats = {"files": 0, "size": 0}
        label = (f"{rel}  ·  "
                 f"{stats['files']} 个文件  ·  "
                 f"约 {_human_size(stats['size'])}")
        items.append({"id": rel, "label": label})
    return ui.select_items(items,
                           title="选择要备份的相册目录（可多选，默认全选）",
                           default_all=True)


# -------------------------- 备份 --------------------------

def backup(dest_dir: Path) -> bool:
    root = _resolve_root()
    logger.info("相册根目录: %s", root)

    # 1) 扫描候选相册
    ui.progress_start("扫描相册目录")
    albums = _scan_album_dirs(root)
    album_by_rel = {a["rel_path"]: a for a in albums}
    ui.progress_done("扫描相册目录", True,
                     f"发现 {len(albums)} 个含媒体文件的目录" if albums
                     else "未发现相册目录，将直接跳过")
    if not albums:
        logger.info("未在 %s 下找到相册目录，跳过相册备份", root)
        return False

    # 2) 二级勾选
    selected = select_albums_interactive(albums)
    if not selected:
        logger.info("用户未勾选任何相册，跳过相册备份")
        return False

    photos_root = dest_dir / "photos"
    photos_root.mkdir(parents=True, exist_ok=True)

    album_entries = []
    total_files = 0
    total_size = 0
    total_skipped = 0

    # 3) 逐相册 → 逐文件拉取
    for rel in selected:
        a = album_by_rel.get(rel)
        if not a:
            logger.warning("选中的相册 rel_path=%s 在扫描结果中不存在，跳过", rel)
            continue
        remote_album = a["remote"]
        local_album = photos_root / rel

        # 列出远端媒体文件
        ui.progress_start(f"扫描 {rel} 的文件列表")
        remote_files = _list_remote_media_files(remote_album)
        ui.progress_done(f"扫描 {rel} 的文件列表", True,
                         f"{len(remote_files)} 个媒体文件")
        if not remote_files:
            ui.progress_done(f"相册 {rel}", True, "无媒体文件，跳过")
            continue

        ui.progress_start(f"相册 {rel}",
                          f"{len(remote_files)} 个文件")
        t0 = time.time()
        album_ok = 0
        album_size = 0
        album_skipped = 0

        for remote_path in remote_files:
            # 相对相册根的子路径（保留子目录结构）
            rel_in_album = posixpath.relpath(remote_path, remote_album)
            local_file = local_album / rel_in_album
            display_name = f"{rel}/{rel_in_album}"

            # 大文件额外输出 start 行，让用户知道正在传输
            file_size = _remote_file_size(remote_path)
            if file_size > LARGE_FILE_THRESHOLD:
                ui.progress_start(f"  📦 {display_name}",
                                   _human_size(file_size))

            ok = _pull_file_with_retry(remote_path, local_file)
            if ok:
                try:
                    actual = local_file.stat().st_size
                except OSError:
                    actual = file_size
                album_ok += 1
                album_size += actual
                ui.progress_done(f"  {display_name}", True,
                                 _human_size(actual))
            else:
                album_skipped += 1
                ui.progress_done(f"  {display_name}", False,
                                 f"重试 {MAX_RETRIES} 次后跳过")

        total_files += album_ok
        total_size += album_size
        total_skipped += album_skipped
        elapsed = time.time() - t0
        detail = (f"{album_ok} 成功, {album_skipped} 跳过, "
                  f"{_human_size(album_size)}, 用时 {elapsed:.1f}s")
        ui.progress_done(f"相册 {rel}", True, detail)
        album_entries.append({
            "remote_root": remote_album,
            "local_relpath": rel,
            "file_count": album_ok,
            "size_bytes": album_size,
            "skipped": album_skipped,
        })

    # 4) 写 manifest
    manifest = {
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "source_root": root,
        "total_files": total_files,
        "total_size_bytes": total_size,
        "total_skipped": total_skipped,
        "albums": album_entries,
    }
    (photos_root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("相册备份完成: %d 个相册, %d 文件, %s, %d 跳过",
                len(album_entries), total_files, _human_size(total_size),
                total_skipped)
    if not album_entries:
        raise RuntimeError("所有选中的相册都备份失败")
    return True


# -------------------------- 恢复 --------------------------

def _trigger_media_scan(rel: str, source_root: str) -> None:
    """尝试触发媒体库扫描；失败静默。"""
    candidate = f"{source_root}/{rel}"
    for intent in [
        (f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
         f"-d {shlex.quote(f'file://{candidate}')}"),
        (f"am broadcast -a android.intent.action.MEDIA_MOUNTED "
         f"-d {shlex.quote(f'file://{source_root}')}"),
    ]:
        proc = adb.run_adb(["shell", intent], check=False)
        if proc.returncode == 0:
            break


def _validated_rel_path(value: object) -> str:
    """验证 manifest 中的相对路径，防止越界读取或写入。"""
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"非法相册路径: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError(f"非法相册路径: {value!r}")
    return path.as_posix()


def restore(src_dir: Path) -> bool:
    manifest_path = src_dir / "photos" / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"未找到 photos/manifest.json: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    target_root = _resolve_root()
    albums = manifest.get("albums") or []
    if not albums:
        logger.info("photos manifest 中无相册条目，跳过")
        return False

    # 二级勾选要恢复的相册（默认全选）
    items = []
    for a in albums:
        rel = _validated_rel_path(a.get("local_relpath"))
        files = a.get("file_count", 0)
        size = a.get("size_bytes", 0)
        label = (f"{rel}  ·  {files} 个文件  ·  "
                 f"{_human_size(size)}")
        items.append({"id": rel, "label": label})
    selected = ui.select_items(
        items, title="选择要恢复的相册（可多选，默认全选）", default_all=True
    )
    if not selected:
        logger.info("用户未勾选任何相册，跳过相册恢复")
        return False
    selected_set = set(selected)

    photos_root = src_dir / "photos"
    total_ok = 0
    total_skipped = 0
    failed_albums: List[str] = []

    for a in albums:
        rel = _validated_rel_path(a.get("local_relpath"))
        if rel not in selected_set:
            continue
        local_album = photos_root / rel
        if not local_album.exists():
            ui.progress_done(f"恢复相册 {rel}", False,
                             f"本地相册目录不存在: {local_album}")
            failed_albums.append(rel)
            continue

        # 列出本地媒体文件
        local_files = _list_local_media_files(local_album)
        if not local_files:
            ui.progress_done(f"恢复相册 {rel}", True, "无媒体文件，跳过")
            continue

        ui.progress_start(f"恢复相册 {rel}",
                          f"{len(local_files)} 个文件")
        album_ok = 0
        album_skipped = 0

        for local_file in local_files:
            # 计算远端目标路径
            rel_in_album = local_file.relative_to(local_album).as_posix()
            remote_file = f"{target_root}/{rel}/{rel_in_album}"
            display_name = f"{rel}/{rel_in_album}"

            # 大文件额外输出 start 行
            try:
                file_size = local_file.stat().st_size
            except OSError:
                file_size = 0
            if file_size > LARGE_FILE_THRESHOLD:
                ui.progress_start(f"  📤 {display_name}",
                                   _human_size(file_size))

            ok = _push_file_with_retry(local_file, remote_file)
            if ok:
                album_ok += 1
                ui.progress_done(f"  {display_name}", True,
                                 _human_size(file_size))
            else:
                album_skipped += 1
                ui.progress_done(f"  {display_name}", False,
                                 f"重试 {MAX_RETRIES} 次后跳过")

        total_ok += album_ok
        total_skipped += album_skipped
        detail = f"{album_ok} 成功, {album_skipped} 跳过"
        ui.progress_done(f"恢复相册 {rel}", True, detail)

        # 尝试触发媒体扫描
        try:
            _trigger_media_scan(rel, target_root)
        except Exception:  # noqa: BLE001
            pass

    if failed_albums:
        raise RuntimeError(
            f"相册恢复部分失败: {', '.join(failed_albums)}; "
            f"成功 {total_ok}, 跳过 {total_skipped}"
        )
    return True
