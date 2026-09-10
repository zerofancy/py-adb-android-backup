#!/usr/bin/env python3
"""Android 备份入口。

通过 PyWebIO 让用户勾选要备份的数据范围，并将选中的数据备份到当前目录下
以操作时间命名的子目录中。若设备未 root，会在页面给出警告，并自动禁用
需要 root 的备份项（应用、联系人、短信）。

支持的备份项：
  - 相册 (photos)：照片与视频，无需 root
  - 联系人 (contacts)：需要 root
  - 短信与彩信 (sms)：需要 root
  - 应用与应用数据 (apps)：需要 root
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List

from pywebio import start_server
from pywebio.output import put_markdown, put_text

from android_backup import adb, apps, contacts, photos, sms, ui

# 注册支持的备份项（附带 requires_root 标记，用于无 root 时禁用）
# 注意：相册无需 root，其它三项（应用/联系人/短信）都需要访问系统私有目录。
SUPPORTED_ITEMS: List[Dict[str, Any]] = [
    {**photos.ITEM,   "requires_root": False},
    {**contacts.ITEM, "requires_root": True},
    {**sms.ITEM,      "requires_root": True},
    {**apps.ITEM,     "requires_root": True},
]
ITEM_BY_ID = {it["id"]: it for it in SUPPORTED_ITEMS}
BACKEND_BY_ID = {
    photos.ITEM["id"]:   photos,
    contacts.ITEM["id"]: contacts,
    sms.ITEM["id"]:      sms,
    apps.ITEM["id"]:     apps,
}

logger = logging.getLogger("android_backup.backup")


def _new_backup_dir() -> Path:
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    p = Path.cwd() / f"backup-{ts}"
    p.mkdir(parents=True, exist_ok=False)
    return p


def main_app() -> None:
    ui.setup_logging()
    try:
        _run()
    finally:
        _schedule_exit()


def _run() -> None:
    put_markdown("# Android 备份工具")
    put_markdown("> 启动后会自动检查设备连接并尝试 `adb root`；若设备未 root，"
                 "应用/联系人/短信 等需要 root 的项将被禁用，相册仍可用。")

    # 1) 设备连通性检查（不要求 root，失败直接退出）
    try:
        serial = adb.ensure_device()
    except Exception as exc:  # noqa: BLE001
        ui.show_error(str(exc))
        return
    put_text(f"设备已连接: {serial}")

    # 2) 尝试提权 + 检查真实 root 权限（失败不退出，降级为无 root 模式）
    adb.try_adb_root()
    has_root = adb.has_root_access()
    if has_root:
        logger.info("已确认具备 root 权限")
    else:
        logger.warning("adb shell 不具备 root 权限，将禁用需要 root 的备份项")
        ui.show_warning(
            "未检测到 root 权限：应用、联系人、短信的备份需要访问系统私有目录"
            "（/data/data 等），已自动禁用上述备份项。相册（无需 root）仍可勾选使用。"
            "请先对设备进行 root 解锁后再使用完整功能。"
        )

    # 3) 构造勾选列表：需要 root 但无权限的项标记为 disabled
    selectable_items: List[Dict[str, Any]] = []
    for it in SUPPORTED_ITEMS:
        entry: Dict[str, Any] = {
            "id": it["id"],
            "label": it["label"],
        }
        if it.get("requires_root") and not has_root:
            entry["disabled"] = True
            entry["label"] = it["label"] + "  （需要 root 权限）"
        selectable_items.append(entry)

    # 4) 选择备份范围
    selected: List[str] = ui.select_items(
        selectable_items, title="选择要备份的数据范围"
    )
    # 再次过滤：防止 PyWebIO 在旧版本下仍可能返回 disabled 项的值
    selected = [
        sid for sid in selected
        if not (ITEM_BY_ID[sid].get("requires_root") and not has_root)
    ]
    if not selected:
        if has_root:
            ui.show_error("未选择任何备份项，已取消")
        else:
            ui.show_error("未选择任何备份项（可能是未 root 导致所有项被禁用），已取消")
        return

    # 5) 创建备份目录
    backup_dir = _new_backup_dir()
    logger.info("创建备份目录: %s", backup_dir)
    put_text(f"备份目录: {backup_dir}")

    # 6) 执行备份 —— 接入实时进度框架，每项开始/结束都在网页上追加输出
    ui.begin_progress_section("备份执行进度")
    rows = []
    items_meta = []
    for item_id in selected:
        item = ITEM_BY_ID[item_id]
        backend = BACKEND_BY_ID[item_id]
        ui.progress_start(item["label"])
        try:
            result = backend.backup(backup_dir)
            if result is False:
                rows.append([item["label"], "⏭️ 已跳过", "未选择可备份内容"])
                ui.progress_done(item["label"], True, "已跳过")
                continue
            detail = ", ".join(item["files"])
            rows.append([item["label"], "✅ 成功", detail])
            items_meta.append({
                "id": item_id,
                "label": item["label"],
                "files": item["files"],
            })
            ui.progress_done(item["label"], True, detail)
        except Exception as exc:  # noqa: BLE001
            logger.exception("备份 %s 失败", item_id)
            rows.append([item["label"], "❌ 失败", str(exc)])
            ui.progress_done(item["label"], False, str(exc))

    # 7) 写入 manifest.json
    manifest = {
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "device_serial": serial,
        "has_root": has_root,
        "items": items_meta,
    }
    manifest_path = backup_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    logger.info("已写入 %s", manifest_path)

    ui.show_result("备份结果", rows)
    put_markdown(f"备份目录: `{backup_dir}`")
    put_markdown("---\n_完成后服务将自动退出，可关闭此页面。_")


def _schedule_exit(delay: float = 1.5) -> None:
    """在后台延时退出整个进程，确保页面已渲染最终结果。"""
    def _bye() -> None:
        logger.info("任务结束，进程退出")
        os._exit(0)
    threading.Timer(delay, _bye).start()


def main() -> None:
    ui.setup_logging()
    logger.info("启动 PyWebIO 服务，请在浏览器中查看页面")
    start_server(main_app, port=0, auto_open_webbrowser=True, debug=False)


if __name__ == "__main__":
    main()
