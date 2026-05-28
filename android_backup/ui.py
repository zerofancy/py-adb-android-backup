"""PyWebIO 交互与控制台日志辅助。"""

from __future__ import annotations

import logging
import sys
from typing import Iterable, List, Mapping

from pywebio.input import checkbox, input as web_input
from pywebio.output import put_markdown, put_table, put_text

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


def select_items(items: Iterable[Mapping[str, str]], title: str,
                 default_all: bool = True) -> List[str]:
    """让用户从 items（含 id/label）中勾选条目，返回所选 id 列表。"""
    items = list(items)
    options = [
        {"label": it["label"], "value": it["id"], "selected": default_all}
        for it in items
    ]
    return checkbox(title, options=options)


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
