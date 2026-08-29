"""PyWebIO 交互与控制台日志辅助。"""

from __future__ import annotations

import logging
import sys
from typing import Any, Iterable, List, Mapping

from pywebio.input import checkbox, input as web_input
from pywebio.output import put_markdown, put_table, put_text
from pywebio.output import Output, popup, put_html, use_scope

_LOG_INITIALIZED = False


def setup_logging() -> None:
    global _LOG_INITIALIZED
    if _LOG_INITIALIZED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s",
                          datefmt="%H:%M:%S")
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)
    _LOG_INITIALIZED = True


def log(msg: str) -> None:
    logging.getLogger("android_backup").info(msg)


def select_items(items: Iterable[Mapping[str, Any]], title: str,
                 default_all: bool = True) -> List[str]:
    """让用户从 items（含 id/label，可选 disabled/selected）中勾选条目，返回所选 id 列表。

    item 支持字段：
        - id: str          (必填) 选项 value
        - label: str       (必填) 显示文案
        - disabled: bool   (可选) 是否灰掉不可选
        - selected: bool   (可选，优先级高于 default_all) 是否默认选中
    """
    items = list(items)
    options: List[Mapping[str, Any]] = []
    for it in items:
        opt: dict = {
            "label": it["label"],
            "value": it["id"],
        }
        if "selected" in it:
            opt["selected"] = bool(it["selected"])
        else:
            opt["selected"] = default_all and not bool(it.get("disabled"))
        if it.get("disabled"):
            opt["disabled"] = True
        options.append(opt)
    return checkbox(title, options=options) or []


def show_warning(msg: str) -> None:
    """在页面上渲染一条醒目的警告横幅（橙黄色背景）。"""
    escaped = (
        msg.replace("&", "&amp;")
           .replace("<", "&lt;")
           .replace(">", "&gt;")
    )
    put_html(
        '<div style="padding:12px 16px;margin:8px 0 16px;border:1px solid '
        '#e6a23c;background:#fdf6ec;border-radius:6px;color:#b88230;'
        'font-size:14px;line-height:1.6;">'
        '⚠️ <b>警告：</b>' + escaped + '</div>'
    )
    logging.getLogger("android_backup").warning(msg)


def show_result(title: str, summary_rows: Iterable[Iterable[str]]) -> None:
    put_markdown(f"## {title}")
    rows = list(summary_rows)
    if rows:
        put_table(rows, header=["项目", "状态", "详情"])
    else:
        put_text("（无）")


def show_error(msg: str) -> None:
    put_markdown(f"### ❌ 出错了\n```\n{msg}\n```")
    logging.getLogger("android_backup").error(msg)


def ask_path(prompt: str) -> str:
    return web_input(prompt, type="text", required=True)
