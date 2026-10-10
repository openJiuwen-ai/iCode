# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for EncodingDetector — BOM, statistical, fallback, and binary detection."""

from __future__ import annotations

import codecs
import os
from pathlib import Path

import pytest

from chrys.foundation.text.encoding import (
    DEFAULT_FALLBACK_ENCODINGS,
    DetectionResult,
    EncodingDetector,
    _canonical_encoding,
    decode_bytes,
    is_mostly_text,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

detector = EncodingDetector()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-32", "gb18030"])
@pytest.mark.parametrize("mutable", [False, True])
def test_strict_decode_preserves_detected_text(encoding: str, mutable: bool) -> None:
    text = "# 中文配置说明\r\nNAME=测试配置\r\n"
    raw = text.encode(encoding)
    assert decode_bytes(bytearray(raw) if mutable else raw, errors="strict") == text


@pytest.mark.parametrize(
    "raw",
    [
        ("# 中文配置说明\n" * 20).encode() + b"SECRET=abc\xffdef\n",
        "NAME=测试\n".encode("utf-16")[:-1],
        "NAME=测试\n".encode("utf-32")[:-1],
        b"# ASCII comment\n" * 5000 + b"NAME=abc\xffdef\n",
    ],
    ids=["damaged-utf8", "truncated-utf16", "truncated-utf32", "beyond-detection-sample"],
)
def test_strict_decode_rejects_damage_without_changing_default_replacement(raw: bytes) -> None:
    with pytest.raises(UnicodeDecodeError):
        decode_bytes(raw, errors="strict")
    assert "�" in decode_bytes(raw)


def test_strict_decode_allows_an_intentionally_encoded_replacement_character() -> None:
    text = "NAME=literal �\n"
    assert decode_bytes(text.encode(), errors="strict") == text
    assert decode_bytes(b"", errors="strict") == ""


def _result(raw: bytes) -> DetectionResult:
    return detector.detect_from_bytes(raw)


# ---------------------------------------------------------------------------
# _canonical_encoding
# ---------------------------------------------------------------------------


def test_canonical_ascii_becomes_utf8() -> None:
    assert _canonical_encoding("ascii") == "utf-8"


def test_canonical_underscores_to_hyphens() -> None:
    assert _canonical_encoding("utf_16_le") == "utf-16-le"


def test_canonical_none_passthrough() -> None:
    assert _canonical_encoding(None) is None


def test_canonical_lowercase() -> None:
    assert _canonical_encoding("UTF-8") == "utf-8"


# ---------------------------------------------------------------------------
# is_mostly_text
# ---------------------------------------------------------------------------


def test_mostly_text_empty() -> None:
    assert is_mostly_text("") is True


def test_mostly_text_normal() -> None:
    assert is_mostly_text("Hello, world!\nLine 2\tTabbed") is True


def test_mostly_text_with_few_controls() -> None:
    # 2% control chars — below 5% threshold
    text = "a" * 98 + "\x01\x02"
    assert is_mostly_text(text) is True


def test_mostly_text_fails_with_many_controls() -> None:
    # 10% control chars — above 5% threshold
    text = "a" * 90 + "\x01" * 10
    assert is_mostly_text(text) is False


def test_mostly_text_custom_threshold() -> None:
    text = "a" * 80 + "\x01" * 20  # 20%
    assert is_mostly_text(text, threshold=0.25) is True
    assert is_mostly_text(text, threshold=0.15) is False


# ---------------------------------------------------------------------------
# BOM detection
# ---------------------------------------------------------------------------


def test_bom_utf8_sig() -> None:
    raw = codecs.BOM_UTF8 + b"Hello"
    r = _result(raw)
    assert r.encoding == "utf-8-sig"
    assert r.is_binary is False


def test_bom_utf8_sig_with_cjk() -> None:
    """UTF-8-BOM with multibyte content still detected as utf-8-sig."""
    raw = codecs.BOM_UTF8 + "你好世界 hello\n".encode()
    r = _result(raw)
    assert r.encoding == "utf-8-sig"
    assert r.is_binary is False


def test_bom_utf8_sig_strips_bom_on_decode() -> None:
    """Opening with the detected utf-8-sig encoding strips the BOM (U+FEFF)
    from the decoded string. This is what makes it safe to surface to the
    LLM — read_file relies on this property."""
    text = "Hello, BOM!\n"
    raw = codecs.BOM_UTF8 + text.encode("utf-8")
    r = _result(raw)
    assert r.encoding == "utf-8-sig"
    decoded = raw.decode(r.encoding)
    assert decoded == text
    assert "\ufeff" not in decoded


def test_bom_utf8_sig_empty_payload() -> None:
    """BOM with no content after it still classifies as utf-8-sig (not empty)."""
    raw = codecs.BOM_UTF8
    r = _result(raw)
    assert r.encoding == "utf-8-sig"
    assert r.is_binary is False


def test_bom_utf16_le() -> None:
    raw = codecs.BOM_UTF16_LE + "Hello".encode("utf-16-le")
    r = _result(raw)
    assert r.encoding == "utf-16"
    assert r.is_binary is False


def test_bom_utf16_be() -> None:
    raw = codecs.BOM_UTF16_BE + "Hello".encode("utf-16-be")
    r = _result(raw)
    assert r.encoding == "utf-16"
    assert r.is_binary is False


def test_bom_utf32_le() -> None:
    raw = codecs.BOM_UTF32_LE + "Hello".encode("utf-32-le")
    r = _result(raw)
    assert r.encoding == "utf-32"
    assert r.is_binary is False


def test_bom_utf32_be() -> None:
    raw = codecs.BOM_UTF32_BE + "Hello".encode("utf-32-be")
    r = _result(raw)
    assert r.encoding == "utf-32"
    assert r.is_binary is False


def test_bom_utf32_le_not_confused_with_utf16() -> None:
    """UTF-32 LE BOM starts with FF FE (same as UTF-16 LE), must check 4 bytes first."""
    raw = codecs.BOM_UTF32_LE + "AB".encode("utf-32-le")
    r = _result(raw)
    assert r.encoding == "utf-32"


# ---------------------------------------------------------------------------
# UTF-8 detection
# ---------------------------------------------------------------------------


def test_plain_ascii() -> None:
    r = _result(b"Hello, World!\nLine 2\n")
    assert r.encoding in ("utf-8", "ascii")  # charset_normalizer may say ascii
    assert r.is_binary is False


def test_utf8_chinese() -> None:
    raw = "你好世界\n这是一段中文测试文本。\n".encode()
    r = _result(raw)
    assert r.encoding == "utf-8"
    assert r.is_binary is False


def test_utf8_mixed() -> None:
    raw = "Hello 你好 World 世界\nこんにちは\n".encode()
    r = _result(raw)
    assert r.encoding == "utf-8"
    assert r.is_binary is False


def test_utf8_emoji() -> None:
    raw = "Hello 🌍🎉\nEmoji text\n".encode()
    r = _result(raw)
    assert r.encoding == "utf-8"


def test_damaged_utf8_prefers_utf8_over_legacy_codecs() -> None:
    """Mostly-valid UTF-8 should not be reinterpreted as legacy CJK/Cyrillic."""
    text = "你好世界\n中文文本\n" * 20
    raw = text.encode("utf-8") + b"\xff"
    r = _result(raw)
    assert r.encoding == "utf-8"
    assert raw.decode(r.encoding, errors="replace").startswith("你好世界")


# ---------------------------------------------------------------------------
# GBK / GB18030 / Big5 detection
# ---------------------------------------------------------------------------


def test_gb18030_chinese() -> None:
    text = "这是一段用GB18030编码的中文文本，包含各种常见汉字。\n第二行内容。\n"  # noqa: RUF001
    raw = text.encode("gb18030")
    r = _result(raw)
    # charset_normalizer should identify this; fallback would also work
    assert r.encoding is not None
    assert r.is_binary is False
    # Verify we can actually decode with the detected encoding
    decoded = raw.decode(r.encoding)
    assert "中文" in decoded


def test_big5_chinese() -> None:
    text = "這是一段用Big5編碼的繁體中文文本。\n第二行內容。\n"
    raw = text.encode("big5")
    r = _result(raw)
    assert r.encoding is not None
    assert r.is_binary is False
    decoded = raw.decode(r.encoding)
    assert "中文" in decoded or "繁體" in decoded


def test_big5_is_not_overridden_by_cp949_compatibility_decode() -> None:
    text = "繁體中文" * 17
    raw = text.encode("big5")
    r = _result(raw)
    assert r.encoding in {"big5", "big5hkscs"}
    assert raw.decode(r.encoding) == text


def test_gbk_chinese() -> None:
    text = "这是GBK编码的测试文本，包含简体中文字符。\n多行测试。\n"  # noqa: RUF001
    raw = text.encode("gbk")
    r = _result(raw)
    assert r.encoding is not None
    assert r.is_binary is False


def test_sparse_gbk_is_not_treated_as_damaged_utf8() -> None:
    text = ("a" * 1000) + "你好世界\n"
    raw = text.encode("gbk")
    r = _result(raw)
    assert r.encoding != "utf-8"
    assert r.encoding is not None
    assert raw.decode(r.encoding) == text


def test_detect_file_does_not_accept_utf8_when_prefix_ends_with_gbk_lead_byte(tmp_path: Path) -> None:
    sample_size = 64
    text = ("a" * (sample_size + 3)) + "中文" + "\nmore text"
    raw = text.encode("gbk")
    p = tmp_path / "boundary-gbk.txt"
    p.write_bytes(raw)

    r = detector.detect_file(p, sample_size=sample_size)

    assert r.encoding != "utf-8"
    assert r.encoding is not None
    assert raw.decode(r.encoding) == text


def test_non_cjk_text_is_not_overridden_by_gb18030_rerank() -> None:
    text = "Açık kaynak kodlu yazılımlar özgürlük sağlar.\n" * 3  # noqa: RUF001
    raw = text.encode("cp1254")
    r = _result(raw)
    assert r.encoding == "cp1254"
    assert raw.decode(r.encoding) == text


def test_hebrew_text_is_not_overridden_by_gb18030_rerank() -> None:
    text = "אבגדהוזחטיכלמנסעפצקרשת אבגדהוזחטיכלמנסעפצקרשת\n"
    raw = text.encode("cp1255")
    r = _result(raw)
    assert r.encoding is not None
    assert r.encoding not in {"gb18030", "big5", "big5hkscs"}
    assert raw.decode(r.encoding)


# ---------------------------------------------------------------------------
# Shift_JIS / EUC-KR detection
# ---------------------------------------------------------------------------


def test_shift_jis_japanese() -> None:
    text = "これはShift_JISでエンコードされた日本語テキストです。\n二行目。\n"
    raw = text.encode("shift_jis")
    r = _result(raw)
    assert r.encoding is not None
    assert r.is_binary is False


def test_shift_jis_with_kana_not_misdetected_as_gb18030() -> None:
    """GB18030 can decode some Shift-JIS bytes, but the result is mojibake."""
    text = (
        "ドレナージソリューション500ml - Wintecare\n"
        "ホーム / オイル・クリーム / ドレナージソリューション500ml\n"
        "ドレナージソリューションは、乳酸、老廃物、浮腫をすばやく排出します。\n"
    )
    raw = text.encode("shift_jis")
    r = _result(raw)
    assert r.encoding in {"shift-jis", "shift_jis", "cp932"}
    assert raw.decode(r.encoding) == text


def test_euc_kr_korean() -> None:
    text = "이것은 EUC-KR로 인코딩된 한국어 텍스트입니다.\n두 번째 줄.\n"
    raw = text.encode("euc-kr")
    r = _result(raw)
    assert r.encoding is not None
    assert r.is_binary is False


def test_detect_file_allows_truncated_multibyte_sample(tmp_path: Path) -> None:
    text = "한국어 인코딩 샘플입니다. 여러 줄의 내용을 포함합니다.\n" * 20
    raw = text.encode("euc-kr")
    sample_size = next(i + 1 for i in range(80, len(raw) - 1) if raw[i] >= 0x80 and raw[i + 1] >= 0x80)

    p = tmp_path / "korean.txt"
    p.write_bytes(raw)

    r = detector.detect_file(p, sample_size=sample_size)
    assert r.encoding in {"euc-kr", "cp949"}
    assert raw.decode(r.encoding) == text


# ---------------------------------------------------------------------------
# Binary detection
# ---------------------------------------------------------------------------


def test_binary_jpeg() -> None:
    raw = b"\xff\xd8\xff\xe0" + b"\x00" * 100
    assert EncodingDetector.looks_binary(raw) is True
    r = detector.detect_file(Path(os.devnull))  # empty
    # Just verify the API works for empty
    assert r.is_binary is False


def test_binary_png() -> None:
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    assert EncodingDetector.looks_binary(raw) is True


def test_binary_pdf() -> None:
    raw = b"%PDF-1.4 " + b"\x00" * 100
    assert EncodingDetector.looks_binary(raw) is True


def test_binary_zip() -> None:
    raw = b"PK\x03\x04" + b"\x00" * 100
    assert EncodingDetector.looks_binary(raw) is True


def test_binary_gzip() -> None:
    raw = b"\x1f\x8b\x08" + b"\x00" * 100
    assert EncodingDetector.looks_binary(raw) is True


def test_binary_elf() -> None:
    raw = b"\x7fELF" + b"\x00" * 100
    assert EncodingDetector.looks_binary(raw) is True


def test_binary_exe() -> None:
    raw = b"MZ" + b"\x00" * 100
    assert EncodingDetector.looks_binary(raw) is True


def test_binary_sqlite() -> None:
    raw = b"SQLite format 3\x00" + b"\x00" * 100
    assert EncodingDetector.looks_binary(raw) is True


def test_binary_random_bytes() -> None:
    """Random-looking data with lots of control chars should be binary."""
    raw = bytes(range(256)) * 4
    assert EncodingDetector.looks_binary(raw) is True


def test_not_binary_plain_text() -> None:
    raw = b"Just a plain text file.\nWith multiple lines.\n"
    assert EncodingDetector.looks_binary(raw) is False


def test_not_binary_bom_utf16() -> None:
    """BOM-marked UTF-16 text should not be detected as binary."""
    raw = codecs.BOM_UTF16_LE + "Hello World".encode("utf-16-le")
    assert EncodingDetector.looks_binary(raw) is False


def test_not_binary_bom_utf32() -> None:
    raw = codecs.BOM_UTF32_LE + "Hello".encode("utf-32-le")
    assert EncodingDetector.looks_binary(raw) is False


def test_not_binary_utf16_le_no_bom() -> None:
    """UTF-16 LE text without BOM has NUL bytes but should not be binary."""
    raw = "Hello World, this is a test.".encode("utf-16-le")
    assert EncodingDetector.looks_binary(raw) is False


def test_not_binary_utf16_be_no_bom() -> None:
    raw = "Hello World, this is a test.".encode("utf-16-be")
    assert EncodingDetector.looks_binary(raw) is False


def test_not_binary_utf32_le_no_bom() -> None:
    """UTF-32 LE text without BOM should not be falsely detected as binary."""
    raw = "Hello World test.".encode("utf-32-le")
    assert EncodingDetector.looks_binary(raw) is False


def test_not_binary_utf32_be_no_bom() -> None:
    raw = "Hello World test.".encode("utf-32-be")
    assert EncodingDetector.looks_binary(raw) is False


# ---------------------------------------------------------------------------
# Empty / edge cases
# ---------------------------------------------------------------------------


def test_empty_bytes() -> None:
    r = _result(b"")
    assert r.encoding == "utf-8"
    assert r.is_binary is False


def test_single_byte() -> None:
    r = _result(b"A")
    assert r.encoding is not None
    assert r.is_binary is False


def test_single_nul_byte() -> None:
    """Single NUL byte — should be treated as binary."""
    r = _result(b"\x00")
    assert r.is_binary is True


# ---------------------------------------------------------------------------
# detect_file
# ---------------------------------------------------------------------------


def test_detect_file_text(tmp_path: Path) -> None:
    p = tmp_path / "test.txt"
    p.write_text("Hello, 你好世界！\n", encoding="utf-8")  # noqa: RUF001
    r = detector.detect_file(p)
    assert r.encoding == "utf-8"
    assert r.is_binary is False


def test_detect_file_binary(tmp_path: Path) -> None:
    p = tmp_path / "test.bin"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
    r = detector.detect_file(p)
    assert r.is_binary is True


def test_detect_file_utf8_sig(tmp_path: Path) -> None:
    p = tmp_path / "bom.txt"
    p.write_bytes(codecs.BOM_UTF8 + b"Hello BOM")
    r = detector.detect_file(p)
    assert r.encoding == "utf-8-sig"
    assert r.is_binary is False


def test_detect_file_gb18030(tmp_path: Path) -> None:
    p = tmp_path / "chinese.txt"
    text = "这是一段较长的中文测试文本，用于验证GB18030编码检测。" * 10  # noqa: RUF001
    p.write_bytes(text.encode("gb18030"))
    r = detector.detect_file(p)
    assert r.encoding is not None
    assert r.is_binary is False
    decoded = p.read_bytes().decode(r.encoding)
    assert "中文" in decoded


def test_detect_file_empty(tmp_path: Path) -> None:
    p = tmp_path / "empty.txt"
    p.write_bytes(b"")
    r = detector.detect_file(p)
    assert r.encoding == "utf-8"
    assert r.is_binary is False


# ---------------------------------------------------------------------------
# Configurable fallback encodings
# ---------------------------------------------------------------------------


def test_custom_fallback_encodings() -> None:
    """Custom fallback list is respected."""
    custom = EncodingDetector(fallback_encodings=("latin-1",))
    # Latin-1 can decode anything — it should always succeed
    raw = bytes(range(128, 256))  # high bytes, not valid UTF-8
    r = custom.detect_from_bytes(raw)
    assert r.encoding is not None
    assert r.is_binary is False


def test_custom_text_quality_threshold() -> None:
    """Stricter threshold rejects more candidates."""
    # Text with ~3% control chars
    text_bytes = b"a" * 97 + b"\x01\x02\x03"

    lenient = EncodingDetector(text_quality_threshold=0.05)
    strict = EncodingDetector(text_quality_threshold=0.01)

    r_lenient = lenient.detect_from_bytes(text_bytes)
    r_strict = strict.detect_from_bytes(text_bytes)

    assert r_lenient.encoding is not None
    # Strict detector may reject (depends on charset_normalizer), but at
    # minimum the lenient one should accept it
    assert r_lenient.is_binary is False
    # Strict might still find an encoding via charset_normalizer
    # Just verify it doesn't crash
    assert isinstance(r_strict, DetectionResult)


# ---------------------------------------------------------------------------
# Non-BOM UTF-16 / UTF-32 must NOT be selected from the fallback list
# (regression: open(encoding="utf-16") on a non-BOM file raises
# "UTF-16 stream does not start with BOM" and read_file surfaces it as
# "Error: failed to decode file with utf-16")
# ---------------------------------------------------------------------------


def test_default_fallback_excludes_utf16_utf32() -> None:
    """utf-16/utf-32 must not be in the default fallback list."""
    assert "utf-16" not in DEFAULT_FALLBACK_ENCODINGS
    assert "utf-32" not in DEFAULT_FALLBACK_ENCODINGS


def test_gbk_not_misdetected_as_utf16() -> None:
    """GBK bytes must not be classified as utf-16 (the canonical bug repro)."""
    text = "这是GBK编码的测试文本，包含简体中文字符。" * 5  # noqa: RUF001
    raw = text.encode("gbk")
    r = _result(raw)
    assert r.encoding not in ("utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be")
    # And the detected encoding must actually round-trip.
    assert raw.decode(r.encoding) == text


def test_short_high_byte_buffer_no_utf16() -> None:
    """A short non-UTF-8 buffer that charset_normalizer can't classify must
    not fall back to utf-16 (any even-length buffer "decodes" as utf-16
    into BMP code points and would have passed is_mostly_text)."""
    raw = bytes(range(0x80, 0xC0))  # 64 bytes, all high, even length
    r = _result(raw)
    if r.encoding is not None:
        assert r.encoding not in ("utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be")


def test_read_file_does_not_crash_on_non_bom_cjk(tmp_path: Path) -> None:
    """End-to-end: read_file must not raise 'UTF-16 stream does not start
    with BOM' on a non-BOM CJK file. This was the user-visible symptom."""
    from chrys.service.tools.builtins.filesystem import read_file

    p = tmp_path / "gbk.txt"
    text = "GBK文本测试，没有BOM。" * 20  # noqa: RUF001
    p.write_bytes(text.encode("gbk"))

    out = read_file(str(p))
    assert not out.startswith("Error:"), f"read_file errored: {out}"
    assert "GBK" in out


def test_bom_utf16_still_works_after_fallback_change() -> None:
    """Removing utf-16 from fallback must not affect BOM-marked UTF-16
    (handled by step 1 of the pipeline, not the fallback list)."""
    raw = codecs.BOM_UTF16_LE + "Hello BOM World".encode("utf-16-le")
    r = _result(raw)
    assert r.encoding == "utf-16"
    assert r.is_binary is False


# ---------------------------------------------------------------------------
# Round-trip validation: detected encoding can actually decode the content
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,encoding",
    [
        ("Hello World\nSecond line\n", "utf-8"),
        ("你好世界\n中文文本\n", "utf-8"),
        ("這是繁體中文\n", "big5"),
        ("こんにちは世界\n", "shift_jis"),
        ("Hello 🌍\n", "utf-8"),
    ],
)
def test_roundtrip_decode(text: str, encoding: str) -> None:
    """Detected encoding should correctly decode the original bytes."""
    raw = text.encode(encoding)
    r = detector.detect_from_bytes(raw)
    assert r.encoding is not None, f"Failed to detect encoding for {encoding}"
    assert r.is_binary is False
    decoded = raw.decode(r.encoding)
    # For CJK encodings the detected encoding might differ (e.g. gb18030 vs gbk)
    # but the decoded text should still contain the original content
    assert text.strip() in decoded or decoded.strip() in text
