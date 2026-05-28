"""WiFi 配置备份/恢复实现。

策略（与之前不同）：除了 `WifiConfigStore.xml` 解析，本版重点改为
**直接 tar 打包 WiFi 系统应用的数据目录**。

涉及目录（设备上不一定都存在，按存在与否分别打包）：
- `/data/misc/wifi`                              （Android 10 及更早主存放点；新版本仍残留少量数据）
- `/data/misc/apexdata/com.android.wifi`         （Android 11+ Wi-Fi mainline 模块数据，含 WifiConfigStore.xml）

备份：
1) 在设备 root shell 中用 `tar -cpf /sdcard/wifi_data_<token>.tar` 把上述
   存在的目录原样打包（保留 owner/perm）。
2) `adb pull` tar 到本地 `wifi/wifi_data.tar`，并附带一份 metadata.json
   记录这次打包时使用的源目录列表。
3) 同时保留旧版 `WifiConfigStore.xml` 与 `networks.json` 作为参考。

恢复：
1) `adb push` tar 到 `/sdcard/`。
2) `cmd wifi set-wifi-enabled disabled` 关闭 WiFi 子系统，避免覆盖时的写冲突。
3) 在 root shell 中 `tar -xpf <tar> -C /` 解包覆盖原目录。
4) 对解开的目录递归执行 `restorecon -R` 恢复 SELinux 上下文；并修正
   owner（system:system）。
5) `cmd wifi set-wifi-enabled enabled` 重新启用 WiFi。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

from . import adb

logger = logging.getLogger("android_backup.wifi")

# 可能的 WiFi 数据目录（按存在与否选择性打包）
WIFI_DATA_DIRS = [
    "/data/misc/wifi",
    "/data/misc/apexdata/com.android.wifi",
]
# 仍单独抽出 WifiConfigStore.xml 作为可读参考
CANDIDATE_XML_PATHS = [
    "/data/misc/apexdata/com.android.wifi/WifiConfigStore.xml",
    "/data/misc/wifi/WifiConfigStore.xml",
]

RELATIVE_TAR = "wifi/wifi_data.tar"
RELATIVE_META = "wifi/wifi_data.meta.json"
RELATIVE_XML = "wifi/WifiConfigStore.xml"
RELATIVE_NETWORKS = "wifi/networks.json"

ITEM = {
    "id": "wifi",
    "label": "WiFi 密码（含应用数据 tar 包）",
    "files": [RELATIVE_TAR, RELATIVE_META, RELATIVE_XML, RELATIVE_NETWORKS],
}


# -------------------------- 公共辅助 --------------------------

def _remote_path_exists(path: str) -> bool:
    proc = adb.run_adb(
        ["shell", f"[ -e {path} ] && echo YES || echo NO"], check=False
    )
    return proc.returncode == 0 and "YES" in proc.stdout


def _detect_xml_path() -> Optional[str]:
    for path in CANDIDATE_XML_PATHS:
        if _remote_path_exists(path):
            return path
    return None


def _strip_quotes(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        return s[1:-1]
    return s


def _parse_networks(xml_path: Path) -> List[Dict]:
    """从 WifiConfigStore.xml 抽取 SSID/PSK/加密类型，仅作参考，不参与恢复。"""
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as exc:
        logger.warning("解析 %s 失败: %s", xml_path, exc)
        return []
    networks: List[Dict] = []
    for wc in tree.getroot().iter("WifiConfiguration"):
        ssid_raw: Optional[str] = None
        psk_raw: Optional[str] = None
        config_key: Optional[str] = None
        for child in wc:
            name = child.get("name")
            if not name or child.tag != "string":
                continue
            if name == "SSID":
                ssid_raw = child.text
            elif name == "PreSharedKey":
                psk_raw = child.text
            elif name == "ConfigKey":
                config_key = child.text
        if not ssid_raw:
            continue
        networks.append({
            "ssid": _strip_quotes(ssid_raw),
            "psk": _strip_quotes(psk_raw),
            "config_key": config_key,
        })
    return networks


# -------------------------- 备份 --------------------------

def backup(dest_dir: Path) -> None:
    """备份 WiFi 数据目录到本地 tar，并附带 XML / networks.json 作为参考。"""
    existing_dirs = [d for d in WIFI_DATA_DIRS if _remote_path_exists(d)]
    if not existing_dirs:
        raise FileNotFoundError(
            "在设备上未找到任何 WiFi 数据目录: " + ", ".join(WIFI_DATA_DIRS)
        )
    logger.info("将打包以下 WiFi 数据目录: %s", ", ".join(existing_dirs))

    token = _dt.datetime.now().strftime("%Y%m%d%H%M%S")
    tmp_tar = f"/sdcard/wifi_data_{token}.tar"
    # tar 以 / 为根，路径写为相对（去掉前导 /），方便 restore 时解包到 / 还原
    rel_paths = [d.lstrip("/") for d in existing_dirs]
    tar_cmd = f"cd / && tar -cpf {tmp_tar} " + " ".join(rel_paths)
    adb.shell(tar_cmd)
    adb.shell(f"chmod 644 {tmp_tar}")

    local_tar = dest_dir / RELATIVE_TAR
    local_meta = dest_dir / RELATIVE_META
    local_tar.parent.mkdir(parents=True, exist_ok=True)
    try:
        adb.pull(tmp_tar, local_tar)
    finally:
        adb.shell(f"rm -f {tmp_tar}", check=False)

    local_meta.write_text(
        json.dumps({"sources": existing_dirs}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("WiFi 数据 tar 备份完成: %s (%d bytes)",
                local_tar, local_tar.stat().st_size)

    # 同时拉一份 XML + 解析 networks.json 作为参考（失败不阻塞主流程）
    try:
        xml_remote = _detect_xml_path()
        if xml_remote:
            tmp_xml = f"/sdcard/WifiConfigStore_{token}.xml"
            adb.shell(f"cp {xml_remote} {tmp_xml}")
            adb.shell(f"chmod 644 {tmp_xml}")
            local_xml = dest_dir / RELATIVE_XML
            try:
                adb.pull(tmp_xml, local_xml)
            finally:
                adb.shell(f"rm -f {tmp_xml}", check=False)
            networks = _parse_networks(local_xml)
            (dest_dir / RELATIVE_NETWORKS).write_text(
                json.dumps(networks, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info("已附加 XML 参考: %s (解析到 %d 个网络)", local_xml, len(networks))
        else:
            logger.info("未找到 WifiConfigStore.xml，跳过参考备份")
    except Exception as exc:  # noqa: BLE001
        logger.warning("附加 XML 备份失败（忽略，不影响 tar 备份）: %s", exc)


# -------------------------- 恢复 --------------------------

def _set_wifi_enabled(enabled: bool) -> None:
    state = "enabled" if enabled else "disabled"
    try:
        adb.shell(f"cmd wifi set-wifi-enabled {state}")
        logger.info("已 %s WiFi", "启用" if enabled else "停用")
    except Exception as exc:  # noqa: BLE001
        logger.warning("`cmd wifi set-wifi-enabled %s` 失败: %s", state, exc)


def restore(src_dir: Path) -> None:
    """将本地 wifi_data.tar 推回设备并解包覆盖 WiFi 数据目录。"""
    local_tar = src_dir / RELATIVE_TAR
    if not local_tar.exists():
        raise FileNotFoundError(f"备份目录中缺少 tar 文件: {local_tar}")

    meta_path = src_dir / RELATIVE_META
    sources: List[str] = []
    if meta_path.exists():
        try:
            sources = json.loads(meta_path.read_text(encoding="utf-8")).get("sources", [])
        except json.JSONDecodeError:
            sources = []

    token = _dt.datetime.now().strftime("%Y%m%d%H%M%S")
    tmp_tar = f"/sdcard/wifi_restore_{token}.tar"
    logger.info("准备恢复 WiFi 数据，源目录列表: %s",
                ", ".join(sources) if sources else "(未知)")
    adb.push(local_tar, tmp_tar)

    # 1) 暂停 WiFi 子系统，降低写冲突
    _set_wifi_enabled(False)
    try:
        # 2) 解包覆盖到 /
        adb.shell(f"cd / && tar -xpf {tmp_tar}")
        logger.info("已解包 tar 到设备根目录")

        # 3) 修复 owner / SELinux 上下文
        for d in sources or WIFI_DATA_DIRS:
            if not _remote_path_exists(d):
                continue
            # owner: WiFi 数据目录通常属于 system:system；若其中有 keystore
            # 等子项被 tar 还原也会带上原 owner，所以这里保守地只对顶层目录
            # 重新 chown 一次。
            adb.shell(f"chown -R system:system {d}", check=False)
            adb.shell(f"restorecon -R {d}", check=False)
            logger.info("已修复 owner / SELinux 上下文: %s", d)
    finally:
        adb.shell(f"rm -f {tmp_tar}", check=False)
        # 4) 重新启用 WiFi
        _set_wifi_enabled(True)

    logger.info(
        "WiFi 数据恢复完成。如未自动连接，可手动重启 WiFi 或重启设备。"
    )
