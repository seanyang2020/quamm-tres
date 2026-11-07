"""模拟 ADB 设备端点（一期不接真实 ADB，见 app/domain/probe.py 的 TODO）。

每个 MockDevice 是一个本地 HTTP 服务，模拟网络 ADB 设备：
- GET /probe 返回 getprop（ro.product.model / ro.serialno / ro.build.version.release）、
  网卡 MAC、/persist/asset_tag
- 可切换 L3 / L2 / L1 / L0 与随机掉线
"""
from __future__ import annotations

import json
import random
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"


def _rand_mac() -> str:
    return ":".join(f"{secrets.randbelow(256):02x}" for _ in range(6))


def _rand_sn() -> str:
    return "SN" + secrets.token_hex(6).upper()


class MockDevice:
    def __init__(
        self,
        model: str = "QM102V",
        mac: str | None = None,
        sn: str | None = None,
        asset_tag: str | None = None,
        android_version: str = "13",
        build_fingerprint: str | None = None,
        port: int = 0,
    ):
        self.model = model
        self.mac = mac or _rand_mac()
        self.sn = sn or _rand_sn()
        self.asset_tag = asset_tag  # None = 无 /persist/asset_tag
        self.android_version = android_version
        self.build_fingerprint = build_fingerprint or f"{model}/release/eng"
        self.level = "L3"
        self.drop_rate = 0.0  # 随机掉线概率
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def _handler(self):
        device = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path.startswith("/probe"):
                    self._probe()
                else:
                    self.send_error(404)

            def _probe(self):
                level = device.level
                if device.drop_rate and random.random() < device.drop_rate:
                    level = "L0"
                if level == "L0":
                    # 模拟断电/关机：连接被直接掐断
                    try:
                        self.connection.shutdown(2)
                        self.connection.close()
                    except OSError:
                        pass
                    return
                if level == "L1":
                    self._json({"level": "L1"})
                elif level == "L2":
                    self._json({"level": "L2"})
                else:
                    self._json(
                        {
                            "level": "L3",
                            "mac": device.mac,
                            "sn": device.sn,
                            "model": device.model,
                            "android_version": device.android_version,
                            "build_fingerprint": device.build_fingerprint,
                            "asset_tag": device.asset_tag,
                        }
                    )

            def _json(self, obj):
                data = json.dumps(obj).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):  # noqa: ANN002
                pass

        return Handler

    def start(self) -> "MockDevice":
        self._server = ThreadingHTTPServer((HOST, self.port), self._handler())
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def set_level(self, level: str) -> None:
        self.level = level

    @property
    def ip(self) -> str:
        return HOST

    @property
    def adb_address(self) -> str:
        return f"{HOST}:{self.port}"


def start_devices(count: int, models: list[str] | None = None, base_asset: str | None = None) -> list[MockDevice]:
    """启动 count 台模拟设备，按 models 循环分配机型。"""
    models = models or ["QM102V"]
    devices = []
    for i in range(count):
        model = models[i % len(models)]
        asset_tag = f"{base_asset}-{i:03d}" if base_asset else None
        devices.append(MockDevice(model=model, asset_tag=asset_tag).start())
    return devices
