"""wirelog 串口 shell 目标解析：COMRes endpoint 优先，兼容老 wireserialhub。"""
from __future__ import annotations

from types import SimpleNamespace

from app.api.wirelog_router import _resolve_shell_target


def _node(**kw):
    defaults = dict(ip="172.16.10.25", caps_json=None, shell_ip=None)
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _binding(**kw):
    defaults = dict(
        endpoint=None, tcp_port=None, shell_ip=None, com_name=None,
        tty_name=None, baud=115200, write_granted_to=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_resolve_prefers_binding_endpoint():
    """(a) COMRes：有 endpoint → 直接用 tcp://172.16.10.25:9210。"""
    node = _node(caps_json={"raw_tcp_port": 9001})  # caps 故意给错，不得覆盖 endpoint
    binding = _binding(
        endpoint="tcp://172.16.10.25:9210",
        tcp_port=9210,
        com_name="COM16",
    )
    host, port, endpoint = _resolve_shell_target(node, binding, slot=1)
    assert (host, port) == ("172.16.10.25", 9210)
    assert endpoint == "tcp://172.16.10.25:9210"


def test_resolve_tcp_port_plus_ip_without_endpoint():
    """(b) 无 endpoint，有 tcp_port + ip → tcp://node.ip:tcp_port。"""
    node = _node(ip="172.16.10.25", caps_json={"raw_tcp_port": 9001})
    binding = _binding(endpoint=None, tcp_port=9211, com_name="COM21")
    host, port, endpoint = _resolve_shell_target(node, binding, slot=2)
    assert (host, port) == ("172.16.10.25", 9211)
    assert endpoint == "tcp://172.16.10.25:9211"


def test_resolve_tcp_port_prefers_binding_shell_ip():
    """tcp_port 路径优先 binding.shell_ip，再退 node.ip。"""
    node = _node(ip="10.0.0.1")
    binding = _binding(tcp_port=9300, shell_ip="172.16.10.99")
    host, port, endpoint = _resolve_shell_target(node, binding, slot=1)
    assert (host, port) == ("172.16.10.99", 9300)
    assert endpoint == "tcp://172.16.10.99:9300"


def test_resolve_legacy_caps_raw_tcp_port():
    """(c) 无 endpoint/tcp_port → caps.raw_tcp_port + slot-1（老 wireserialhub）。"""
    node = _node(ip="192.168.1.10", caps_json={"raw_tcp_port": 9001})
    binding = _binding()  # 全空
    host, port, endpoint = _resolve_shell_target(node, binding, slot=3)
    assert (host, port) == ("192.168.1.10", 9003)
    assert endpoint == "tcp://192.168.1.10:9003"


def test_resolve_legacy_without_binding():
    """无 binding 行时仍按 caps + slot 推导。"""
    node = _node(ip="192.168.1.10", caps_json=None)  # 默认 9001
    host, port, endpoint = _resolve_shell_target(node, None, slot=1)
    assert (host, port) == ("192.168.1.10", 9001)
    assert endpoint == "tcp://192.168.1.10:9001"
