#!/usr/bin/env python3
"""Android 恢复入口。

用法：
    python restore.py <备份目录>

若未提供备份目录，将在 PyWebIO 页面要求用户输入。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import List, Optional

from pywebio import start_server
from pywebio.output import put_markdown, put_text

from android_backup import adb, apps, ui, wifi

BACKEND_BY_ID = {wifi.ITEM["id"]: wifi, apps.ITEM["id"]: apps}

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
    put_markdown("> 假定设备已 root；启动后会自动执行 `adb root` 与设备就绪检查。")

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

    items = manifest.get("items", [])
    # 仅保留当前版本支持的 backend
    supported = [it for it in items if it.get("id") in BACKEND_BY_ID]
    if not supported:
        ui.show_error("manifest.json 中没有当前工具支持恢复的项")
        return

    # 2) 设备检查
    try:
        serial = adb.ensure_root_device()
    except Exception as exc:  # noqa: BLE001
        ui.show_error(str(exc))
        return
    put_text(f"目标设备: {serial}")

    # 3) 选择恢复范围
    selected: List[str] = ui.select_items(supported, title="选择要恢复的数据范围")
    if not selected:
        ui.show_error("未选择任何恢复项，已取消")
        return

    # 4) 执行恢复
    rows = []
    for item_id in selected:
        item = next(it for it in supported if it["id"] == item_id)
        backend = BACKEND_BY_ID[item_id]
        try:
            backend.restore(backup_dir)
            rows.append([item["label"], "✅ 成功", "完成"])
        except Exception as exc:  # noqa: BLE001
            logger.exception("恢复 %s 失败", item_id)
            rows.append([item["label"], "❌ 失败", str(exc)])

    ui.show_result("恢复结果", rows)
    if any(it == "wifi" for it in selected):
        put_markdown("> 提示：已通过 tar 解包覆盖 WiFi 应用数据目录，并重启 WiFi 子系统。如未自动连接，可手动重启 WiFi 或重启设备。")
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
