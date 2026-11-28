"""v0.54.8（#8436）：设备默认名解析与新建设备默认名规则。"""
from __future__ import annotations

import pytest

from app.domain.catalog import device_probe, device_register
from app.domain.device_naming import (
    SOURCE_EXPLICIT,
    SOURCE_FALLBACK,
    SOURCE_LEGACY_NOTE,
    SOURCE_PRODUCT_DISPLAY,
    name_warning_for_type,
    normalize,
    resolve_device_display_name,
)
from app.models import Device, ProductType
from app.utils import utcnow
from tests.mock_device import MockDevice


# ---------- 纯函数 ----------

def test_explicit_wins():
    name, src = resolve_device_display_name(
        explicit_display_name="我的价签",
        legacy_note="旧备注",
        product_display_name="彩屏价签_V2",
        model_class="esl2_skw",
        position_code="302-R2-05",
    )
    assert (name, src) == ("我的价签", SOURCE_EXPLICIT)


def test_legacy_note_second():
    name, src = resolve_device_display_name(
        legacy_note="技术部用机", product_display_name="彩屏价签_V2",
        model_class="esl2_skw", position_code="302",
    )
    assert (name, src) == ("技术部用机", SOURCE_LEGACY_NOTE)


def test_product_display_name_third():
    name, src = resolve_device_display_name(
        product_display_name="彩屏价签_V2", model_class="esl2_skw",
        position_code="302",
    )
    assert (name, src) == ("彩屏价签_V2", SOURCE_PRODUCT_DISPLAY)


def test_type_code_equal_display_falls_back():
    # 类型展示名等于 type_code：不能当可读名，回退 model-position
    name, src = resolve_device_display_name(
        product_display_name="esl2_skw", model_class="esl2_skw",
        position_code="302-R2-05",
    )
    assert (name, src) == ("esl2_skw-302-R2-05", SOURCE_FALLBACK)


def test_no_model_no_position_returns_none():
    name, src = resolve_device_display_name(model_class=None, position_code=None)
    assert name is None and src == SOURCE_FALLBACK


def test_model_without_position():
    name, src = resolve_device_display_name(model_class="esl2_skw", position_code=None)
    assert (name, src) == ("esl2_skw", SOURCE_FALLBACK)


def test_whitespace_normalized():
    name, _ = resolve_device_display_name(explicit_display_name="  彩屏  价签  V2  ")
    assert name == "彩屏 价签 V2"
    assert normalize("  a \t b  ") == "a b"
    assert normalize("") is None and normalize("   ") is None


def test_blank_explicit_treated_as_missing():
    name, src = resolve_device_display_name(
        explicit_display_name="   ", product_display_name="彩屏价签_V2",
        model_class="esl2_skw", position_code="1",
    )
    assert (name, src) == ("彩屏价签_V2", SOURCE_PRODUCT_DISPLAY)


def test_truncated_to_128():
    name, _ = resolve_device_display_name(explicit_display_name="x" * 300)
    assert len(name) == 128


# ---------- 防呆提示 ----------

def test_name_warning_empty_or_same_as_code():
    assert name_warning_for_type("esl2_skw", "") is not None
    assert name_warning_for_type("esl2_skw", "esl2_skw") is not None
    assert name_warning_for_type("esl2_skw", "彩屏价签_V2") is None


# ---------- 集成：device_register 默认名 ----------

def _make_type(session, code="esl2_skw", display="彩屏价签_V2"):
    session.add(ProductType(
        type_code=code, display_name=display, has_screen=True, has_led=False,
        serial_count=1, adb_port=5555, enabled=True, created_at=utcnow(),
    ))
    session.commit()


def test_register_new_device_uses_product_display_name(db):
    s = db.session()
    try:
        _make_type(s)
        dev = MockDevice(model="esl2_skw").start()
        probe = device_probe(s, ip=dev.ip, port=dev.port, source="mock")
        out = device_register(
            s, probe["form_id"], position_code="302-R2-05", owner="技术部",
            actor_email="admin@corp.com", model_class="esl2_skw", agent_info="pytest/1",
        )
        assert out["display_name"] == "彩屏价签_V2"
        assert out["display_name_source"] == SOURCE_PRODUCT_DISPLAY
        assert s.get(Device, out["device_id"]).display_name == "彩屏价签_V2"
    finally:
        s.close()


def test_register_explicit_display_name_wins(db):
    s = db.session()
    try:
        _make_type(s)
        dev = MockDevice(model="esl2_skw").start()
        probe = device_probe(s, ip=dev.ip, port=dev.port, source="mock")
        out = device_register(
            s, probe["form_id"], position_code="302", owner="技术部",
            actor_email="admin@corp.com", model_class="esl2_skw",
            display_name="前台样机", agent_info="pytest/1",
        )
        assert (out["display_name"], out["display_name_source"]) == ("前台样机", SOURCE_EXPLICIT)
    finally:
        s.close()


def test_register_legacy_note_still_works(db):
    s = db.session()
    try:
        _make_type(s)
        dev = MockDevice(model="esl2_skw").start()
        probe = device_probe(s, ip=dev.ip, port=dev.port, source="mock")
        out = device_register(
            s, probe["form_id"], position_code="302", owner="技术部",
            actor_email="admin@corp.com", model_class="esl2_skw",
            note="技术部备用机", agent_info="pytest/1",
        )
        assert (out["display_name"], out["display_name_source"]) == ("技术部备用机", SOURCE_LEGACY_NOTE)
    finally:
        s.close()


def test_register_fallback_when_type_name_is_type_code(db):
    s = db.session()
    try:
        _make_type(s, display="esl2_skw")
        dev = MockDevice(model="esl2_skw").start()
        probe = device_probe(s, ip=dev.ip, port=dev.port, source="mock")
        out = device_register(
            s, probe["form_id"], position_code="302-R2-05", owner="技术部",
            actor_email="admin@corp.com", model_class="esl2_skw", agent_info="pytest/1",
        )
        assert (out["display_name"], out["display_name_source"]) == (
            "esl2_skw-302-R2-05", SOURCE_FALLBACK,
        )
    finally:
        s.close()


def test_register_existing_device_not_renamed_by_note(db):
    s = db.session()
    try:
        _make_type(s)
        dev = MockDevice(model="esl2_skw").start()
        probe = device_probe(s, ip=dev.ip, port=dev.port, source="mock")
        out = device_register(
            s, probe["form_id"], position_code="302", owner="技术部",
            actor_email="admin@corp.com", model_class="esl2_skw",
            display_name="自定义名", agent_info="pytest/1",
        )
        # 同一设备再次注册：旧 note / 空名不覆盖；显式名才更新
        probe2 = device_probe(s, ip=dev.ip, port=dev.port, source="mock")
        out2 = device_register(
            s, probe2["form_id"], position_code="303", owner="技术部",
            actor_email="admin@corp.com", model_class="esl2_skw",
            note="不要覆盖我", agent_info="pytest/1",
        )
        assert out2["matched_as"] == "existing"
        assert out2["display_name_source"] == "kept"
        assert s.get(Device, out["device_id"]).display_name == "自定义名"
        probe3 = device_probe(s, ip=dev.ip, port=dev.port, source="mock")
        out3 = device_register(
            s, probe3["form_id"], position_code="303", owner="技术部",
            actor_email="admin@corp.com", model_class="esl2_skw",
            display_name="新名字", agent_info="pytest/1",
        )
        assert s.get(Device, out["device_id"]).display_name == "新名字"
    finally:
        s.close()
