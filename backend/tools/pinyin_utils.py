"""backend/tools/pinyin_utils.py

Chinese name to pinyin conversion (pypinyin wrapper).
Used for the metadata.json 'speaker' field.
"""
from __future__ import annotations
from pypinyin import lazy_pinyin, Style


def to_pinyin(chinese_name: str) -> str:
    """Convert Chinese characters to lowercase pinyin without separators.
    Non-Chinese characters are passed through unchanged.
    Example: '任无弦' → 'renwuxian'"""
    if not chinese_name:
        return chinese_name
    parts = lazy_pinyin(chinese_name, style=Style.NORMAL)
    return "".join(parts).lower()


def is_chinese(text: str) -> bool:
    """Return True if text contains any CJK Unified Ideograph characters."""
    return any("一" <= ch <= "鿿" for ch in text)
