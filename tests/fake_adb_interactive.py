#!/usr/bin/env python3
"""Fake adb 客户端：模拟两种 adbd 行为，供 prober_real 交互式回退单测。

环境变量：
  FAKE_ADB_MODE=interactive  — 忽略 ``shell <cmd>`` 参数，只开交互式 shell（变体 A）
  FAKE_ADB_MODE=normal       — ``shell <cmd>`` 直接执行命令（变体 B，默认）
  FAKE_ADB_PRODUCT / FAKE_ADB_ETH_MAC / FAKE_ADB_WLAN_MAC — 伪造采集值
"""
from __future__ import annotations

import os
import sys


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


PRODUCT = _env("FAKE_ADB_PRODUCT", "nxf7e")
ETH_MAC = _env("FAKE_ADB_ETH_MAC", "")  # 空 = 无 eth0
WLAN_MAC = _env("FAKE_ADB_WLAN_MAC", "dc:0d:30:07:01:00")
MODE = _env("FAKE_ADB_MODE", "normal").lower()


def _run_shell_command(cmd: str) -> int:
    """在伪造的设备 rootfs 上执行一条 shell 命令，结果写 stdout。"""
    cmd = (cmd or "").strip()
    if not cmd:
        return 0
    # 复合命令：echo MARK; real; 或 test && echo
    if cmd.startswith("echo ") and ";" not in cmd and "&&" not in cmd:
        print(cmd[5:].strip().strip("'").strip('"'))
        return 0
    if cmd.startswith("echo ") and "&&" not in cmd:
        # echo __TRES_M0__ 等
        print(cmd[5:].strip().strip("'").strip('"'))
        return 0
    if "product_name" in cmd:
        if PRODUCT:
            print(PRODUCT)
        else:
            print("cat: can't open '/etc/product_name': No such file or directory", file=sys.stderr)
        return 0
    if "eth0/address" in cmd:
        if ETH_MAC:
            print(ETH_MAC)
        else:
            print(
                "cat: can't open '/sys/class/net/eth0/address': No such file or directory",
                file=sys.stderr,
            )
        return 0
    if "wlan0/address" in cmd:
        if WLAN_MAC:
            print(WLAN_MAC)
        else:
            print(
                "cat: can't open '/sys/class/net/wlan0/address': No such file or directory",
                file=sys.stderr,
            )
        return 0
    if "/proc/cmdline" in cmd:
        print("console=ttyS0 root=/dev/mtdblock0 spi_nor")
        return 0
    if "QUAMM_WIFI_OK" in cmd or "wlan0 &&" in cmd:
        if WLAN_MAC or "wlan0" in cmd:
            print("QUAMM_WIFI_OK")
        return 0
    if "device-tree/compatible" in cmd:
        print("quamm,nxf7e")
        return 0
    if "fw_version" in cmd:
        print("V1.00.01.100")
        return 0
    if "/etc/version" in cmd:
        print("1.0.0")
        return 0
    if "build-info" in cmd:
        print("project=nxf7e\nplatform=qm\nbuilt_at=2026-01-01")
        return 0
    if "config.gz" in cmd or "CONFIG_PRODUCT_" in cmd:
        print("CONFIG_PRODUCT_NXF7E=y\nCONFIG_QM_VERSION=\"1.0\"\nCONFIG_WIFI_SUPPORT=y")
        return 0
    if "/proc/cpuinfo" in cmd:
        print(
            "processor\t: 0\n"
            "Features\t: half thumb fastmult vfp edsp neon vfpv3\n"
            "CPU implementer\t: 0x41\n"
            "CPU part\t: 0xc07\n"
        )
        return 0
    if cmd == "exit" or cmd.startswith("exit"):
        return 0
    # 未知命令：静默成功（对齐 busybox cat 失败也常 rc=0 的宽松口径）
    return 0


def _interactive_session() -> int:
    """读 stdin 逐行执行；每行前打印 ``~ # `` 提示符（对齐变体设备）。"""
    sys.stdout.write("~ # ")
    sys.stdout.flush()
    for line in sys.stdin:
        line = line.rstrip("\n\r")
        # 回显命令（真实交互 shell 常见）
        if line:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        if line.strip() in ("exit", "exit\r"):
            return 0
        _run_shell_command(line)
        sys.stdout.write("~ # ")
        sys.stdout.flush()
    return 0


def main(argv: list[str]) -> int:
    # 剥掉外层 GNU timeout（prober 会包 timeout N <this> ...）
    args = argv[1:]
    if not args:
        return 0

    if args[0] == "connect":
        print(f"connected to {args[1] if len(args) > 1 else 'device'}")
        return 0
    if args[0] == "disconnect":
        return 0
    if args[0] == "devices":
        print("List of devices attached")
        return 0

    # adb -s <addr> shell ...
    if args[0] == "-s" and len(args) >= 2:
        args = args[2:]

    if not args or args[0] != "shell":
        return 0

    shell_args = args[1:]
    if MODE in ("interactive", "ignores_args", "a"):
        # 变体 A：有 shell 参数时忽略之，只吐提示符（对齐 ``adb shell echo`` → ``~ #``）
        # 无参数时读 stdin 做交互会话（prober 回退路径：Popen + communicate）
        if shell_args:
            print("~ #")
            return 0
        return _interactive_session()

    # 变体 B：正常执行 shell 参数；无参数则交互
    if not shell_args:
        return _interactive_session()
    # adb shell 可能是 ["shell", "echo x"] 或 ["shell", "echo", "x"]
    cmd = " ".join(shell_args)
    return _run_shell_command(cmd)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
