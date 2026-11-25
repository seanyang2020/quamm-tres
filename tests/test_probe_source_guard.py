"""P0：探测来源闸门 — 生产禁止 mock 静默落库；既有 mock 可见 simulated。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import settings
from app.constants import DeviceStatus, HealthLevel, ProbeSource
from app.domain.catalog import (
    DomainError,
    _RegistrationForm,
    _public_device,
    _resolve_probe_source,
    device_probe,
    device_register,
    form_store,
)
from app.domain.probe import MockProber, RealXosProber, build_prober
from app.models import AuditEvent, Device
from app.utils import new_form_id, utcnow
from tests.mock_device import MockDevice


def test_device_probe_rejects_explicit_mock_by_default(db, monkeypatch):
    monkeypatch.setattr(settings, "allow_mock_probe", False)
    monkeypatch.setattr(settings, "probe_backend", "real")
    s = db.session()
    try:
        with pytest.raises(DomainError) as ei:
            device_probe(s, ip="127.0.0.1", port=1, source="mock")
        assert ei.value.code == "MOCK_PROBE_DISABLED"
        assert "生产禁止 mock" in ei.value.message
        assert "real" in ei.value.message
    finally:
        s.close()


def test_device_probe_rejects_default_mock_backend_when_disallowed(db, monkeypatch):
    monkeypatch.setattr(settings, "allow_mock_probe", False)
    monkeypatch.setattr(settings, "probe_backend", "mock")
    s = db.session()
    try:
        with pytest.raises(DomainError) as ei:
            device_probe(s, ip="127.0.0.1", port=1)  # source=None → probe_backend
        assert ei.value.code == "MOCK_PROBE_DISABLED"
    finally:
        s.close()


def test_device_probe_allows_mock_when_flag_enabled(db, monkeypatch):
    monkeypatch.setattr(settings, "allow_mock_probe", True)
    monkeypatch.setattr(settings, "probe_backend", "real")
    dev = MockDevice(model="QM102V").start()
    s = db.session()
    try:
        out = device_probe(s, ip=dev.ip, port=dev.port, source="mock")
        assert out["probe"]["source"] == ProbeSource.MOCK
        assert out["probe"]["level"] == HealthLevel.L3
    finally:
        s.close()
        dev.stop()


def test_device_register_rejects_mock_form_when_disallowed(db, monkeypatch):
    monkeypatch.setattr(settings, "allow_mock_probe", False)
    now = utcnow()
    form = _RegistrationForm(
        form_id=new_form_id(),
        ip="10.0.0.1",
        port=5555,
        probe={
            "ip": "10.0.0.1",
            "adb_port": 5555,
            "source": ProbeSource.MOCK,
            "level": HealthLevel.L3,
            "mac": "aa:bb:cc:dd:ee:01",
            "sn": "SN-MOCK-REG-01",
            "model": "QM102V",
        },
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        agent_info="pytest-agent/1.0",
    )
    form_store.put(form)
    s = db.session()
    try:
        with pytest.raises(DomainError) as ei:
            device_register(
                s,
                form_id=form.form_id,
                position_code="302-R9-01",
                owner="owner@corp.com",
                actor_email="user01@corp.com",
                agent_info="pytest-agent/1.0",
            )
        assert ei.value.code == "MOCK_PROBE_DISABLED"
    finally:
        s.close()


def test_build_prober_unknown_raises_value_error():
    assert isinstance(build_prober("mock"), MockProber)
    assert isinstance(build_prober("real"), RealXosProber)
    assert isinstance(build_prober(None), RealXosProber)
    with pytest.raises(ValueError, match="illegal probe_backend"):
        build_prober("unknown")
    with pytest.raises(ValueError, match="not-a-backend"):
        build_prober("not-a-backend")


def test_register_real_device_source_not_mock(db, monkeypatch):
    monkeypatch.setattr(settings, "allow_mock_probe", False)
    monkeypatch.setattr(settings, "probe_backend", "real")
    now = utcnow()
    form = _RegistrationForm(
        form_id=new_form_id(),
        ip="10.0.0.2",
        port=5555,
        probe={
            "ip": "10.0.0.2",
            "adb_port": 5555,
            "source": ProbeSource.REAL,
            "level": HealthLevel.L3,
            "mac": "aa:bb:cc:dd:ee:02",
            "sn": "SN-REAL-REG-01",
            "model": "QM102V",
        },
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        agent_info="pytest-agent/1.0",
    )
    form_store.put(form)
    s = db.session()
    try:
        out = device_register(
            s,
            form_id=form.form_id,
            position_code="302-R9-02",
            owner="owner@corp.com",
            actor_email="user01@corp.com",
            agent_info="pytest-agent/1.0",
        )
        d = s.get(Device, out["device_id"])
        assert d is not None
        assert d.source == ProbeSource.REAL
        assert d.source != ProbeSource.MOCK
        assert d.simulated is False
    finally:
        s.close()


def test_registration_audit_detail_includes_source(db, monkeypatch):
    monkeypatch.setattr(settings, "allow_mock_probe", False)
    now = utcnow()
    form = _RegistrationForm(
        form_id=new_form_id(),
        ip="10.0.0.3",
        port=5555,
        probe={
            "ip": "10.0.0.3",
            "adb_port": 5555,
            "source": ProbeSource.REAL,
            "level": HealthLevel.L3,
            "mac": "aa:bb:cc:dd:ee:03",
            "sn": "SN-REAL-AUDIT-01",
            "model": "QM102V",
        },
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        agent_info="pytest-agent/1.0",
    )
    form_store.put(form)
    s = db.session()
    try:
        out = device_register(
            s,
            form_id=form.form_id,
            position_code="302-R9-03",
            owner="owner@corp.com",
            actor_email="user01@corp.com",
            agent_info="pytest-agent/1.0",
        )
        row = (
            s.query(AuditEvent)
            .filter(AuditEvent.device_id == out["device_id"], AuditEvent.action == "register")
            .order_by(AuditEvent.created_at.desc())
            .first()
        )
        assert row is not None
        assert row.detail.get("source") == ProbeSource.REAL
        assert row.detail.get("simulated") is False
    finally:
        s.close()


def test_public_device_marks_existing_mock_as_simulated(db):
    s = db.session()
    try:
        d = Device(
            device_id="dev_mock_sim_01",
            display_name="mock-sim",
            model_class=None,
            mac="aa:bb:cc:dd:ee:99",
            sn="SN-MOCK-SIM-01",
            position_code="lab-mock-01",
            owner="owner@corp.com",
            status=DeviceStatus.AVAILABLE,
            health_level=HealthLevel.L3,
            source=ProbeSource.MOCK,
            ip="127.0.0.1",
            adb_port=5555,
            created_at=utcnow(),
        )
        s.add(d)
        s.commit()
        view = _public_device(d, s, for_agent=True)
        assert view["source"] == ProbeSource.MOCK
        assert view["simulated"] is True
        admin = _public_device(d, s, for_agent=False)
        assert admin["source"] == ProbeSource.MOCK
        assert admin["simulated"] is True
    finally:
        s.close()


def test_resolve_probe_source_helpers(monkeypatch):
    monkeypatch.setattr(settings, "allow_mock_probe", False)
    monkeypatch.setattr(settings, "probe_backend", "real")
    assert _resolve_probe_source(None) == ProbeSource.REAL
    assert _resolve_probe_source(" REAL ") == ProbeSource.REAL
    with pytest.raises(DomainError) as ei:
        _resolve_probe_source("mock")
    assert ei.value.code == "MOCK_PROBE_DISABLED"
    with pytest.raises(DomainError) as ei2:
        _resolve_probe_source("nope")
    assert ei2.value.code == "INVALID_PROBE_SOURCE"
