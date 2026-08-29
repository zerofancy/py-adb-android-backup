"""ADB 命令封装。

所有 adb 命令的执行入口集中在本模块，统一打印控制台日志。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("android_backup.adb")


class AdbError(RuntimeError):
    """adb 命令执行失败时抛出。"""


def _adb_binary() -> str:
    adb = shutil.which("adb")
    if not adb:
        raise AdbError("未找到 adb 命令，请先安装 Android Platform-Tools 并加入 PATH")
    return adb


def run_adb(args: List[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    """执行一条 adb 命令。

    Args:
        args: 不含 "adb" 前缀的参数列表。
        check: 失败时是否抛出 AdbError。
        capture: 是否捕获 stdout/stderr 文本。
    """
    cmd = [_adb_binary(), *args]
    logger.info("$ %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        check=False,
        text=True,
        capture_output=capture,
    )
    if proc.stdout:
        for line in proc.stdout.rstrip().splitlines():
            logger.info("  out| %s", line)
    if proc.stderr:
        for line in proc.stderr.rstrip().splitlines():
            logger.info("  err| %s", line)
    if check and proc.returncode != 0:
        raise AdbError(
            f"adb 命令失败: {' '.join(cmd)} (exit={proc.returncode})\n{proc.stderr.strip()}"
        )
    return proc


def list_devices() -> List[str]:
    """返回处于 `device` 状态的序列号列表。"""
    proc = run_adb(["devices"], check=True)
    serials: List[str] = []
    for line in proc.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def ensure_device() -> str:
    """确保有且仅一台设备在线（不要求 root），返回设备序列号。"""
    devices = list_devices()
    if not devices:
        raise AdbError("未检测到已连接的设备，请确认 USB 调试已开启并已授权")
    logger.info("检测到设备: %s", ", ".join(devices))
    if len(devices) > 1:
        raise AdbError(
            f"检测到多台设备 ({devices})，本工具一次只支持一台设备，请断开其它设备"
        )
    return devices[0]


def try_adb_root() -> bool:
    """尝试执行 `adb root` 提权，成功返回 True，失败返回 False（不抛异常）。"""
    proc = run_adb(["root"], check=False, capture=True)
    if proc.returncode != 0:
        logger.info("adb root 执行失败: %s", proc.stderr.strip() or "(无输出)")
        return False
    # 某些设备上 adb root 成功但需要等守护进程切换
    wait = run_adb(["wait-for-device"], check=False, capture=True)
    if wait.returncode != 0:
        logger.info("adb wait-for-device 失败: %s", wait.stderr.strip())
        return False
    msg = (proc.stdout or proc.stderr or "").strip()
    if "cannot" in msg.lower() or "unable" in msg.lower() or "secure" in msg.lower():
        # 典型失败信息：adbd cannot run as root in production builds
        logger.info("adb root 被拒绝: %s", msg)
        return False
    logger.info("adb root 成功: %s", msg or "(守护进程已切换)")
    return True


def has_root_access() -> bool:
    """检测当前 adb shell 是否具备 root 权限（uid == 0）。

    前提：已连接到设备。不抛异常，仅返回布尔值。
    """
    proc = run_adb(["shell", "id -u"], check=False, capture=True)
    if proc.returncode != 0:
        return False
    out = proc.stdout.strip()
    if out == "0":
        return True
    # 兜底：直接尝试访问 /data/misc（这是 WiFi 备份用到的目录）
    probe = run_adb(
        ["shell", "ls /data/misc >/dev/null 2>&1 && echo YES || echo NO"],
        check=False, capture=True,
    )
    return probe.returncode == 0 and "YES" in probe.stdout


def ensure_root_device() -> str:
    """确保有且仅一台已 root 的设备就绪，返回设备序列号。

    会尝试 adb root 并验证权限；失败时抛 AdbError。
    """
    serial = ensure_device()

    # 提权（用户已具备 root 前置条件）
    if not try_adb_root():
        raise AdbError(
            "adb root 失败：设备可能未解锁 root 或运行在 production 构建。"
            "可继续使用工具，但需要 root 的备份项将被禁用。"
        )

    devices = list_devices()
    if not devices:
        raise AdbError("adb root 后设备未恢复在线，请重新插拔设备或检查授权")

    if not has_root_access():
        raise AdbError(
            "adb shell 不具备 root 权限（uid != 0）。"
            "可继续使用工具，但需要 root 的备份项将被禁用。"
        )
    return devices[0]


def shell(cmd: str, check: bool = True) -> str:
    """通过 `adb shell` 执行命令并返回 stdout 文本。"""
    proc = run_adb(["shell", cmd], check=check)
    return proc.stdout


def pull(remote: str, local: Path) -> None:
    """从设备拉取文件到本地。"""
    local.parent.mkdir(parents=True, exist_ok=True)
    run_adb(["pull", remote, str(local)], check=True)
    if not local.exists():
        raise AdbError(f"adb pull 完成但本地文件不存在: {local}")
    logger.info("已拉取 %s -> %s (%d bytes)", remote, local, local.stat().st_size)


def push(local: Path, remote: str) -> None:
    """将本地文件推送到设备。"""
    if not local.exists():
        raise AdbError(f"待推送文件不存在: {local}")
    run_adb(["push", str(local), remote], check=True)
    logger.info("已推送 %s -> %s (%d bytes)", local, remote, local.stat().st_size)
