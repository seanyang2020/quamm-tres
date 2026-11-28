"""设备默认名解析（Redmine #8436）。

新建设备未填名称时，默认使用设备类型的可读展示名；优先级：

explicit display_name > legacy note > product_display_name（非空且不等于
model_class）> fallback > f"{model_class}-{position_code}"

所有输入做空白规范化（首尾去空格、内部连续空白折叠），结果截断 128 字符；
空字符串视为未提供。
"""
from __future__ import annotations

MAX_LEN = 128

# display_name_source 取值
SOURCE_EXPLICIT = "explicit"
SOURCE_LEGACY_NOTE = "legacy_note"
SOURCE_PRODUCT_DISPLAY = "product_display"
SOURCE_FALLBACK = "fallback"


def normalize(value) -> str | None:
    """空白规范化：None/空串返回 None；否则折叠连续空白并去首尾。"""
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def _clean(value) -> str | None:
    """规范化 + 截断；空视为未提供。"""
    text = normalize(value)
    if text is None:
        return None
    return text[:MAX_LEN]


def resolve_device_display_name(
    explicit_display_name=None,
    legacy_note=None,
    product_display_name=None,
    model_class=None,
    position_code=None,
    fallback=None,
) -> tuple[str | None, str]:
    """返回 (display_name, display_name_source)。

    product_display_name 仅在 strip 后非空且不等于 model_class（避免把
    技术 type_code 当可读名）时生效。兜底为 f"{model_class}-{position_code}"；
    两者都缺时给 None。
    """
    explicit = _clean(explicit_display_name)
    if explicit:
        return explicit, SOURCE_EXPLICIT

    note = _clean(legacy_note)
    if note:
        return note, SOURCE_LEGACY_NOTE

    product = _clean(product_display_name)
    model = normalize(model_class)
    if product and product != model:
        return product, SOURCE_PRODUCT_DISPLAY

    fb = _clean(fallback)
    if fb:
        return fb, SOURCE_FALLBACK

    if model:
        pos = normalize(position_code)
        return (f"{model}-{pos}" if pos else model), SOURCE_FALLBACK
    return None, SOURCE_FALLBACK


def name_warning_for_type(type_code, display_name) -> str | None:
    """设备类型名称防呆：display_name 为空或等于 type_code 时给提示（不拒绝）。"""
    code = normalize(type_code)
    name = normalize(display_name)
    if not code:
        return None
    if not name or name == code:
        return (
            f"设备类型 {code} 的名称为空或与 type_code 相同，"
            "新建设备的默认名称会退化为技术代号；建议改为人类可读名称（如 彩屏价签_V2）"
        )
    return None
