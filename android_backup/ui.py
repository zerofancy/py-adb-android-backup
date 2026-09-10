"""PyWebIO 交互与控制台日志辅助。"""

from __future__ import annotations

import logging
import sys
from typing import Any, Iterable, List, Mapping

from pywebio.input import checkbox, input as web_input
from pywebio.output import put_markdown, put_table, put_text
from pywebio.output import Output, popup, put_html, put_scrollable, use_scope

_LOG_INITIALIZED = False
_PROGRESS_SCOPE = "progress_stream"
_PROGRESS_INNER_SCOPE = "progress_stream_inner"


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


# -------------------------- 实时进度输出 helper --------------------------

def _html_escape(s: Any) -> str:
    if s is None:
        return ""
    text = str(s)
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def begin_progress_section(title: str = "执行进度") -> None:
    """在网页中新建一块"实时进度"区域，后续进度都追加到这里末尾。

    区域为固定高度的可滚动容器，内容更新时自动滚动到底部（keep_bottom=True）。
    """
    put_markdown(f"## {title}")
    # 外层 scope：重入调用时整体清空
    with use_scope(_PROGRESS_SCOPE, clear=True):
        # 固定高度 + keep_bottom 自动滚到底部；内部再包一个 scope 用于追加行
        with put_scrollable(height=420, keep_bottom=True):
            # 先创建/清空内部 scope 作为行容器
            with use_scope(_PROGRESS_INNER_SCOPE, clear=True):
                pass


def progress_start(item: str, note: str = "") -> None:
    """标记一个条目开始执行（灰色 ⏱ 行）。"""
    safe_item = _html_escape(item)
    safe_note = _html_escape(note)
    suffix = f" <span style='color:#999;'>— {safe_note}</span>" if safe_note else ""
    with use_scope(_PROGRESS_INNER_SCOPE):
        put_html(
            f"<div style='padding:3px 2px;color:#6b7280;font-size:14px;"
            f"line-height:1.7;'>"
            f"⏱ 开始 <b style='color:#374151;'>{safe_item}</b>{suffix}</div>"
        )


def progress_done(item: str, ok: bool, detail: str = "") -> None:
    """标记一个条目结束：ok=True 绿色 ✅，ok=False 红色 ❌。"""
    safe_item = _html_escape(item)
    safe_detail = _html_escape(detail)
    if ok:
        color, emoji = "#16a34a", "✅"
    else:
        color, emoji = "#dc2626", "❌"
    suffix = (f" <span style='color:#4b5563;font-weight:normal;'>"
              f"— {safe_detail}</span>") if safe_detail else ""
    with use_scope(_PROGRESS_INNER_SCOPE):
        put_html(
            f"<div style='padding:3px 2px;color:{color};font-size:14px;"
            f"line-height:1.7;font-weight:600;'>"
            f"{emoji} <span style='color:#111827;'>{safe_item}</span>"
            f": 完成{suffix}</div>"
        )
