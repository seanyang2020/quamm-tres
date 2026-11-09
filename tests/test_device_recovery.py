import sys
sys.path.insert(0, "/home/scm/quamm-res")

import pytest
from app.domain import device_recovery as rec


def test_seq_4digit_2223():
    assert rec.generate_knock_sequence("2223") == [2223, 2232, 2322, 3222]


def test_seq_4digit_3223():
    assert rec.generate_knock_sequence("3223") == [3223, 2233, 2332, 3322]


def test_seq_3digit_adds_1000():
    assert rec.generate_knock_sequence("322") == [1322, 1223, 1232]


def test_seq_5digit_five_rotations():
    assert rec.generate_knock_sequence("12345") == [12345, 23451, 34512, 45123, 51234]


def test_seq_6digit():
    seq = rec.generate_knock_sequence("123456")
    assert len(seq) == 5 and seq[0] == 123456


def test_seq_invalid_chars():
    with pytest.raises(ValueError):
        rec.generate_knock_sequence("22a3")


def test_seq_invalid_length():
    with pytest.raises(ValueError):
        rec.generate_knock_sequence("22")
    with pytest.raises(ValueError):
        rec.generate_knock_sequence("2222222")


def test_knock_already_up(monkeypatch):
    rec.reset_cooldown()
    monkeypatch.setattr(rec, "tcp_open", lambda *a, **k: True)
    out = rec.knock_recover("1.2.3.4", "2223")
    assert out["already_up"] and out["adb_ready"]


def test_knock_recovers(monkeypatch):
    rec.reset_cooldown()
    states = {"n": 0}
    def fake_tcp(host, port, timeout=1.0):
        states["n"] += 1
        return states["n"] > 1  # 首次检查关闭，敲门后开放
    sent = []
    monkeypatch.setattr(rec, "tcp_open", fake_tcp)
    monkeypatch.setattr(rec, "_send_knock", lambda h, p, interval=0.5: sent.append(p))
    out = rec.knock_recover("1.2.3.4", "2223", wait_timeout=5)
    assert out["recovered"] and out["adb_ready"]
    assert sent == [[2223, 2232, 2322, 3222]]


def test_knock_timeout(monkeypatch):
    rec.reset_cooldown()
    monkeypatch.setattr(rec, "tcp_open", lambda *a, **k: False)
    monkeypatch.setattr(rec, "_send_knock", lambda *a, **k: None)
    out = rec.knock_recover("1.2.3.4", "2223", wait_timeout=1)
    assert not out["recovered"] and not out["adb_ready"] and "error" in out


def test_knock_cooldown(monkeypatch):
    rec.reset_cooldown()
    monkeypatch.setattr(rec, "tcp_open", lambda *a, **k: False)
    monkeypatch.setattr(rec, "_send_knock", lambda *a, **k: None)
    rec.knock_recover("1.2.3.4", "2223", wait_timeout=1)
    out2 = rec.knock_recover("1.2.3.4", "2223", wait_timeout=1)
    assert out2["skipped"] == "cooldown"


def test_knock_bad_eventcode(monkeypatch):
    rec.reset_cooldown()
    monkeypatch.setattr(rec, "tcp_open", lambda *a, **k: False)
    out = rec.knock_recover("1.2.3.4", "xx", use_cooldown=False)
    assert not out["adb_ready"] and "error" in out
