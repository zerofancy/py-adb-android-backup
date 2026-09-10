#!/usr/bin/env python3
"""Android 恢复入口。

用法：
    python restore.py <备份目录>

若未提供备份目录，将在 PyWebIO 页面要求用户输入。
若目标设备未 root，会在页面给出警告，并自动禁用需要 root 的恢复项
（应用、联系人、短信）；相册恢复仍可正常使用。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from pywebio import start_server
from pywebio.output import put_markdown, put_text

from android_backup import adb, apps, contacts, photos, sms, ui

# 每个备份项是否需要 root 才能恢复
ITEM_REQUIRES_ROOT: Dict[str, bool] = {
    photos.ITEM["id"]:   False,
    contacts.ITEM["id"]: True,
    sms.ITEM["id"]:      True,
    apps.ITEM["id"]:     True,
}
BACKEND_BY_ID = {
    photos.ITEM["id"]:   photos,
    contacts.ITEM["id"]: contacts,
    sms.ITEM["id"]:      sms,
    apps.ITEM["id"]:     apps,
}

logger = logging.getLogger("android_backup.restore")


def _load_manifest(backup_dir: Path) -> dict:
    if not backup_dir.exists() or not backup_dir.is_dir():
        raise ValueError(f"备份目录不存在或不是目录: {backup_dir}")
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.exists():
        raise ValueError(f"目录中缺少 manifest.json: {manifest_path}")
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"manifest.json 解析失败: {exc}") from exc


def _make_app(initial_dir: Optional[str]):
    def app() -> None:
        ui.setup_logging()
        try:
            _run(initial_dir)
        finally:
            _schedule_exit()

    return app


def _run(initial_dir: Optional[str]) -> None:
    put_markdown("# Android 恢复工具")
    put_markdown("> 启动后会自动检查设备连接并尝试 `adb root`；若设备未 root，"
                 "应用/联系人/短信 等需要 root 的项将被禁用，相册恢复仍可用。")

    # 1) 获取备份目录
    backup_dir_str = initial_dir or ui.ask_path("请输入备份目录路径")
    backup_dir = Path(backup_dir_str).expanduser().resolve()
    try:
        manifest = _load_manifest(backup_dir)
    except Exception as exc:  # noqa: BLE001
        ui.show_error(str(exc))
        return
    put_text(f"备份目录: {backup_dir}")
    put_text(f"备份创建时间: {manifest.get('created_at', '?')}")
    put_text(f"原设备 serial: {manifest.get('device_serial', '?')}")
    backup_has_root = bool(manifest.get("has_root", True))
    if not backup_has_root:
        logger.info("本次备份是在无 root 环境下创建的")

    items = manifest.get("items", [])
    # 仅保留当前版本支持的 backend
    supported = [it for it in items if it.get("id") in BACKEND_BY_ID]
    if not supported:
        ui.show_error("manifest.json 中没有当前工具支持恢复的项")
        return

    # 2) 设备连通性检查（不要求 root，失败直接退出）
    try:
        serial = adb.ensure_device()
    except Exception as exc:  # noqa: BLE001
        ui.show_error(str(exc))
        return
    put_text(f"目标设备: {serial}")

    # 3) 尝试提权 + 检查真实 root 权限（失败不退出，降级为无 root 模式）
    adb.try_adb_root()
    has_root = adb.has_root_access()
    if has_root:
        logger.info("已确认具备 root 权限")
    else:
        logger.warning("adb shell 不具备 root 权限，将禁用需要 root 的恢复项")
        ui.show_warning(
            "未检测到 root 权限：应用、联系人、短信的恢复需要写入系统私有目录"
            "（/data/data 等），已自动禁用上述恢复项。相册恢复仍可勾选使用。"
            "请先对设备进行 root 解锁后再使用完整功能。"
        )

    # 4) 构造勾选列表：需要 root 但无权限的项标记为 disabled
    selectable_items: List[Dict[str, Any]] = []
    for it in supported:
        item_id = it["id"]
        entry: Dict[str, Any] = {
            "id": item_id,
            "label": it.get("label") or item_id,
        }
        if ITEM_REQUIRES_ROOT.get(item_id, False) and not has_root:
            entry["disabled"] = True
            entry["label"] = (it.get("label") or item_id) + "  （需要 root 权限）"
        selectable_items.append(entry)

    # 5) 选择恢复范围
    selected: List[str] = ui.select_items(
        selectable_items, title="选择要恢复的数据范围"
    )
    # 再次过滤：防止 PyWebIO 旧版本下 disabled 项仍可能被返回
    selected = [
        sid for sid in selected
        if not (ITEM_REQUIRES_ROOT.get(sid, False) and not has_root)
    ]
    if not selected:
        if has_root:
            ui.show_error("未选择任何恢复项，已取消")
        else:
            ui.show_error("未选择任何恢复项（可能是未 root 导致所有项被禁用），已取消")
        return

    # 6) 执行恢复 —— 接入实时进度框架，每项开始/结束都在网页上追加输出
    ui.begin_progress_section("恢复执行进度")
    rows = []
    for item_id in selected:
        item = next(it for it in supported if it["id"] == item_id)
        backend = BACKEND_BY_ID[item_id]
        label = item.get("label") or item_id
        ui.progress_start(label)
        try:
            result = backend.restore(backup_dir)
            if result is False:
                rows.append([label, "⏭️ 已跳过", "未选择可恢复内容"])
                ui.progress_done(label, True, "已跳过")
                continue
            rows.append([label, "✅ 成功", "完成"])
            ui.progress_done(label, True, "完成")
        except Exception as exc:  # noqa: BLE001
            logger.exception("恢复 %s 失败", item_id)
            rows.append([label, "❌ 失败", str(exc)])
            ui.progress_done(label, False, str(exc))

    ui.show_result("恢复结果", rows)
    put_markdown("---\n_完成后服务将自动退出，可关闭此页面。_")


def _schedule_exit(delay: float = 1.5) -> None:
    """在后台延时退出整个进程，确保页面已渲染最终结果。"""
    def _bye() -> None:
        logger.info("任务结束，进程退出")
        os._exit(0)
    threading.Timer(delay, _bye).start()


def main() -> None:
    ui.setup_logging()
    initial_dir = sys.argv[1] if len(sys.argv) > 1 else None
    if initial_dir:
        logger.info("使用命令行参数指定的备份目录: %s", initial_dir)
    logger.info("启动 PyWebIO 服务，请在浏览器中查看页面")
    start_server(_make_app(initial_dir), port=0,
                 auto_open_webbrowser=True, debug=False)


if __name__ == "__main__":
    main()
