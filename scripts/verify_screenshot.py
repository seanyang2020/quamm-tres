"""最小验证：对真实硬浮点设备截图并确认 pull 回主机的 jpg 可打开。

用法（在仓库根目录，需 adb 可达设备）：
    timeout 120 .venv/bin/python scripts/verify_screenshot.py
    timeout 120 .venv/bin/python scripts/verify_screenshot.py --ip 172.16.10.246 --quality 70

判定：float_abi=hard / bin=vxosdbg，且本地 JPEG 具备 SOI(FFD8FF)/EOI(FFD9) 标记、
字节数 > 100、imghdr 识别为 jpeg。任一不满足 exit 1。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许从仓库根目录直接运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import screenshot  # noqa: E402


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """扫 SOF0/SOF2 段取宽高；能解析出尺寸说明是对得上格式的 JPEG。"""
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            h = (data[i + 5] << 8) | data[i + 6]
            w = (data[i + 7] << 8) | data[i + 8]
            return w, h
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seglen = (data[i + 2] << 8) | data[i + 3]
        if seglen < 2:
            break
        i += 2 + seglen
    return None


def jpeg_openable(path: str) -> tuple[bool, str]:
    p = Path(path)
    if not p.is_file():
        return False, "本地文件不存在"
    data = p.read_bytes()
    if len(data) <= 100:
        return False, f"文件过小（{len(data)} bytes）"
    if data[:3] != b"\xff\xd8\xff":
        return False, "缺少 JPEG SOI(FFD8FF) 标记"
    if data[-2:] != b"\xff\xd9":
        return False, "缺少 JPEG EOI(FFD9) 标记"
    dim = _jpeg_dimensions(data)
    if dim is None:
        return False, "未解析到 SOF 尺寸段"
    return True, f"有效 JPEG（{len(data)} bytes，{dim[0]}x{dim[1]}，SOI/EOI/SOF 完整）"


def main() -> int:
    ap = argparse.ArgumentParser(description="设备截图最小验证")
    ap.add_argument("--ip", default="172.16.10.246", help="设备 IP")
    ap.add_argument("--port", type=int, default=5555, help="adb 端口")
    ap.add_argument("--quality", type=int, default=60, help="JPEG 质量 1-100")
    ap.add_argument("--rotate", type=int, default=0, help="旋转 0/90/180/270")
    ap.add_argument("--device-id", default=None, help="可选：按 device_id 截图（走库读 ip/port + 机型浮点配置）")
    args = ap.parse_args()

    print(f"[verify] 目标 ip={args.ip}:{args.port} device_id={args.device_id} "
          f"quality={args.quality} rotate={args.rotate}", flush=True)
    try:
        if args.device_id:
            result = screenshot.capture_screenshot(
                device_id=args.device_id, quality=args.quality, rotate=args.rotate
            )
        else:
            result = screenshot.capture_screenshot(
                ip=args.ip, port=args.port, quality=args.quality, rotate=args.rotate
            )
    except Exception as e:  # noqa: BLE001 - 统一给出可读失败信息
        print(f"[verify] FAIL 截图异常：{type(e).__name__}: {e}")
        return 1

    print("[verify] 返回：" + json.dumps(result, ensure_ascii=False))
    ok, why = jpeg_openable(result["local_path"])
    print(f"[verify] JPEG 校验：{'OK' if ok else 'FAIL'} - {why}")

    hardfloat_ok = result["float_abi"] == "hard" and result["bin"] == "vxosdbg"
    print(f"[verify] 硬浮点判定：{'OK' if hardfloat_ok else 'FAIL'} "
          f"(float_abi={result['float_abi']}, bin={result['bin']}, source={result['float_source']})")

    passed = ok and hardfloat_ok and result["bytes"] > 0
    print("[verify] " + ("PASS：截图成功且 pull 回主机 jpg 可打开" if passed else "FAIL"))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
