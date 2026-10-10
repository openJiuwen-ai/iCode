# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Robust file encoding detection.

Detection strategy:
1. BOM detection (deterministic)
2. Strong UTF-8 preference, including isolated-byte damage
3. Statistical analysis via ``charset_normalizer``
4. Legacy CJK reranking for common cross-decoding mojibake
5. Fallback candidates with text quality validation

Every candidate is verified by decoding + text quality check before being
accepted.
"""

from __future__ import annotations

import codecs
import contextlib
import functools
from dataclasses import dataclass
from typing import TYPE_CHECKING

from charset_normalizer import from_bytes

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from typing import Literal

_DETECT_SAMPLE_BYTES = 64 * 1024  # 64 KiB probe window
_DETECT_TAIL_BYTES = 4  # Enough to complete any UTF-8 sequence that starts at the sample edge.

BytesLike = bytes | bytearray | memoryview

# Byte values that are suspicious in text files (C0 controls minus common whitespace).
_SUSPICIOUS_CTRL_BYTES = frozenset(set(range(32)) - {8, 9, 10, 12, 13})

# Common binary file signatures.
_BINARY_SIGNATURES = (
    b"\xff\xd8\xff",  # JPEG
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"%PDF-",  # PDF
    b"PK\x03\x04",  # ZIP / DOCX / XLSX / JAR
    b"\x1f\x8b",  # GZIP
    b"\x7fELF",  # ELF
    b"MZ",  # PE (EXE/DLL)
    b"SQLite format 3\x00",  # SQLite
    b"GIF87a",  # GIF87
    b"GIF89a",  # GIF89
    b"RIFF",  # RIFF (WAV/AVI/WebP)
)

# All known BOM prefixes — used to quickly rule out binary for BOM-marked text.
_BOM_PREFIXES: tuple[bytes, ...] = (
    b"\xef\xbb\xbf",  # UTF-8
    b"\xff\xfe\x00\x00",  # UTF-32 LE
    b"\x00\x00\xfe\xff",  # UTF-32 BE
    b"\xff\xfe",  # UTF-16 LE
    b"\xfe\xff",  # UTF-16 BE
)

# Default fallback encoding candidates (order matters).
# UTF-16 and UTF-32 are intentionally excluded — without a BOM they produce
# false positives (any even-length buffer "decodes" cleanly into BMP code
# points and passes is_mostly_text), and Python's ``open(encoding="utf-16")``
# then raises ``UnicodeError: UTF-16 stream does not start with BOM`` on the
# first read. BOM-marked UTF-16/32 is handled deterministically by
# ``_bom_hint`` before this list is consulted.
DEFAULT_FALLBACK_ENCODINGS: tuple[str, ...] = ("utf-8", "gb18030", "big5", "shift_jis", "euc-kr")

# Extra legacy CJK candidates used only for high-confidence reranking. They are
# intentionally limited to codecs with kana/Hangul evidence. GB18030 and Big5
# are too permissive: unrelated single-byte text can form valid Han mojibake.
_LEGACY_CJK_RERANK_ENCODINGS: tuple[str, ...] = ("shift-jis", "cp932", "euc-kr", "cp949")
_CHINESE_CJK_ENCODINGS = frozenset({"big5", "big5hkscs", "big5-hkscs", "gb18030", "gbk", "gb2312", "cp936"})


@dataclass(slots=True)
class DetectionResult:
    """Result of encoding detection.

    Attributes:
        encoding: Detected encoding name (None if binary).
        is_binary: Whether the content appears to be binary data.
    """

    encoding: str | None
    is_binary: bool


class EncodingDetector:
    """Robust encoding detector with configurable fallback candidates.

    Usage::

        detector = EncodingDetector()

        # From file
        result = detector.detect_file(Path("data.csv"))
        if not result.is_binary:
            text = Path("data.csv").read_text(encoding=result.encoding)

        # From bytes
        result = detector.detect_from_bytes(raw_bytes)
    """

    def __init__(
        self,
        fallback_encodings: Sequence[str] = DEFAULT_FALLBACK_ENCODINGS,
        *,
        text_quality_threshold: float = 0.05,
    ) -> None:
        """
        Args:
            fallback_encodings: Ordered encoding candidates tried when statistical
                detection fails.
            text_quality_threshold: Maximum ratio of suspicious control characters
                allowed in decoded text (lower = stricter).
        """
        self._fallback_encodings = _with_system_ansi_encoding(tuple(fallback_encodings))
        self._text_quality_threshold = text_quality_threshold

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect_from_bytes(self, raw: bytes) -> DetectionResult:
        """Detect encoding from a byte buffer."""
        return self._detect_from_bytes(raw, final=True)

    def _detect_from_bytes(self, raw: bytes, *, final: bool) -> DetectionResult:
        """Detect encoding from bytes.

        ``final=False`` means *raw* is a prefix sample of a larger buffer.
        Candidate decoders may then buffer one incomplete trailing character
        instead of rejecting an otherwise-valid multibyte encoding.
        """
        if not raw:
            return DetectionResult("utf-8", False)

        # 1) BOM — deterministic
        bom_enc = self._bom_hint(raw)
        if bom_enc:
            return DetectionResult(bom_enc, False)

        # 2) UTF-8 fast path — if data is valid UTF-8 with good quality,
        #    prefer it over statistical detection (avoids charset_normalizer
        #    misidentifying UTF-8 emoji/CJK as legacy CJK encodings).
        if self._decodes_as_text(raw, "utf-8", final=True):
            return DetectionResult("utf-8", False)

        # 3) Tolerant UTF-8 preference — a UTF-8 file with a handful of
        #    corrupt bytes is safer to edit as UTF-8 than as a permissive
        #    legacy codec (GB18030/cp1251/etc.) that would mojibake every
        #    valid multibyte character and then write the mojibake back.
        if self._looks_like_utf8_with_few_errors(raw):
            return DetectionResult("utf-8", False)

        # 4) charset_normalizer — statistical (for non-UTF-8 data)
        cn_result = self._try_charset_normalizer(raw, final=final)
        if cn_result:
            return cn_result

        cjk_result = self._try_legacy_cjk_rerank(raw, final=final)
        if cjk_result:
            return DetectionResult(cjk_result, False)

        # 5) Fallback candidates — try each, validate text quality
        for enc in self._fallback_encodings:
            if self._decodes_as_text(raw, enc, final=_candidate_decode_final(enc, sample_final=final)):
                return DetectionResult(enc, False)

        # 6) Nothing worked
        return DetectionResult(None, True)

    def detect_file(self, path: Path, sample_size: int = _DETECT_SAMPLE_BYTES) -> DetectionResult:
        """Detect encoding from a file (reads only a sample).

        Binary detection is performed first; if the sample looks binary the
        method short-circuits and returns ``is_binary=True``.
        """
        with path.open("rb") as f:
            raw = f.read(sample_size)
            raw += f.read(_DETECT_TAIL_BYTES)
            sample_is_prefix = bool(f.read(1))

        if not raw:
            return DetectionResult("utf-8", False)

        # Quick binary rejection via signatures (before BOM/encoding work)
        if self._has_binary_signature(raw):
            return DetectionResult(None, True)

        return self._detect_from_bytes(raw, final=not sample_is_prefix)

    def decode(self, raw: bytes | bytearray, *, errors: Literal["strict", "replace"] = "replace") -> str:
        """Detect encoding and decode *raw* bytes to str.

        No binary check is performed — this is intended for data already
        known to be text (e.g. subprocess output).  Falls back to UTF-8
        with replacement if detection fails or the detected encoding
        cannot decode the full buffer.

        ``errors="strict"`` uses the same detection but refuses undecodable
        bytes anywhere in the full buffer. Configuration readers use this
        mode so a replacement character cannot silently change a value or
        be written back over the original bytes.
        """
        if not raw:
            return ""
        sample = bytes(raw[: _DETECT_SAMPLE_BYTES + _DETECT_TAIL_BYTES])
        result = self._detect_from_bytes(sample, final=len(raw) <= len(sample))
        enc = result.encoding or "utf-8"
        if errors == "strict":
            return raw.decode(enc)
        try:
            return raw.decode(enc)
        except UnicodeDecodeError, LookupError:
            return raw.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # Binary detection
    # ------------------------------------------------------------------

    @staticmethod
    def looks_binary(sample: bytes) -> bool:
        """Heuristic: does the byte sample look like binary data?

        Checks binary signatures, BOM markers, NUL-byte patterns, and
        suspicious control character ratios.  Handles UTF-16 and UTF-32
        NUL patterns to avoid false positives on valid Unicode text.
        """
        if len(sample) > _DETECT_SAMPLE_BYTES:
            sample = sample[:_DETECT_SAMPLE_BYTES]

        if not sample:
            return False

        # Known binary signatures
        if EncodingDetector._has_binary_signature(sample):
            return True

        # BOM-marked text is never binary
        if sample.startswith(_BOM_PREFIXES):
            return False

        # NUL bytes need special handling: could be UTF-16/32 text or true binary
        if b"\x00" in sample:
            _probe = EncodingDetector._nul_text_probe
            return not any(_probe(sample, enc) for enc in ("utf-32-le", "utf-32-be", "utf-16-le", "utf-16-be"))

        # General control-character heuristic
        non_text = sum(1 for b in sample if b in _SUSPICIOUS_CTRL_BYTES)
        return non_text > 0 and (non_text / len(sample)) > 0.30

    @staticmethod
    def _has_binary_signature(sample: bytes) -> bool:
        return any(sample.startswith(sig) for sig in _BINARY_SIGNATURES)

    @staticmethod
    def _nul_text_probe(sample: bytes, encoding: str) -> bool:
        """Try decoding NUL-containing bytes as ``encoding`` and check text quality."""
        try:
            text = sample.decode(encoding)
        except UnicodeDecodeError, LookupError:
            return False
        return is_mostly_text(text)

    # ------------------------------------------------------------------
    # BOM detection
    # ------------------------------------------------------------------

    @staticmethod
    def _bom_hint(raw: BytesLike) -> str | None:
        """Return canonical encoding if a BOM is present.

        Returns ``"utf-16"`` / ``"utf-32"`` (not LE/BE variants) so that
        ``open(encoding=...)`` auto-handles the BOM correctly.
        """
        b = raw if isinstance(raw, (bytes, bytearray)) else bytes(raw)
        # UTF-32 must be checked before UTF-16 (UTF-32 LE BOM starts with UTF-16 LE BOM)
        if b[:3] == codecs.BOM_UTF8:
            return "utf-8-sig"
        if b[:4] in (codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE):
            return "utf-32"
        if b[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
            return "utf-16"
        return None

    # ------------------------------------------------------------------
    # Statistical detection
    # ------------------------------------------------------------------

    def _try_charset_normalizer(self, raw: bytes, *, final: bool) -> DetectionResult | None:
        """Run charset_normalizer and validate the best result."""
        if not final and _has_only_sparse_trailing_high_bytes(raw):
            return None

        with contextlib.suppress(Exception):
            result = from_bytes(raw)
            best = result.best()
            if best and best.encoding:
                enc = _canonical_encoding(best.encoding)
                if enc and self._decodes_as_text(raw, enc, final=_candidate_decode_final(enc, sample_final=final)):
                    reranked = self._try_legacy_cjk_rerank(raw, final=final, preferred=enc)
                    return DetectionResult(reranked or enc, False)

            # Try other candidates from charset_normalizer
            for match in result:
                if not match.encoding:
                    continue
                enc = _canonical_encoding(match.encoding)
                if enc and self._decodes_as_text(raw, enc, final=_candidate_decode_final(enc, sample_final=final)):
                    reranked = self._try_legacy_cjk_rerank(raw, final=final, preferred=enc)
                    return DetectionResult(reranked or enc, False)

        return None

    # ------------------------------------------------------------------
    # Text quality validation
    # ------------------------------------------------------------------

    def _decodes_as_text(self, raw: bytes, enc: str, *, final: bool = True) -> bool:
        """Return True if ``raw`` decodes as ``enc`` with acceptable text quality."""
        try:
            text = _decode_candidate(raw, enc, final=final)
        except UnicodeDecodeError, LookupError, ValueError:
            return False
        return is_mostly_text(text, threshold=self._text_quality_threshold)

    def _try_legacy_cjk_rerank(self, raw: bytes, *, final: bool, preferred: str | None = None) -> str | None:
        """Pick a CJK legacy codec when script evidence is strong.

        The rerank only votes for codecs with direct script evidence:
        Japanese kana for Shift-JIS/cp932 and Hangul for EUC-KR/cp949.
        Chinese codecs are not scored here because GB18030/Big5 can decode
        unrelated byte streams into valid-looking Han mojibake.

        When charset_normalizer already preferred a Chinese codec, only
        Japanese kana evidence or standard EUC-KR evidence may override it.
        CP949-only compatibility decodes are intentionally ignored in that
        case so valid Big5/GB18030 text is not stolen by a Korean superset.
        """
        scores: dict[str, float] = {}

        for enc in _LEGACY_CJK_RERANK_ENCODINGS:
            try:
                text = _decode_candidate(raw, enc, final=final)
            except UnicodeDecodeError, LookupError, ValueError:
                continue
            if not is_mostly_text(text, threshold=self._text_quality_threshold):
                continue
            score = _legacy_cjk_script_score(text, enc)
            if score > 0:
                scores[enc] = score

        if not scores:
            return None

        preferred = _canonical_encoding(preferred)
        japanese = {enc: score for enc, score in scores.items() if enc in {"shift-jis", "cp932"}}
        korean = {enc: score for enc, score in scores.items() if enc in {"euc-kr", "cp949"}}

        if preferred in _CHINESE_CJK_ENCODINGS:
            if japanese:
                return max(japanese, key=lambda enc: (japanese[enc], -_LEGACY_CJK_RERANK_ENCODINGS.index(enc)))
            if "euc-kr" in korean:
                return max(korean, key=lambda enc: (korean[enc], -_LEGACY_CJK_RERANK_ENCODINGS.index(enc)))
            return None

        if preferred in scores and preferred in {"shift-jis", "cp932", "euc-kr", "cp949"} and scores[preferred] >= 3.0:
            best_score = max(scores.values())
            if best_score - scores[preferred] <= 0.75:
                return preferred

        if "cp949" in scores and "euc-kr" not in scores and japanese:
            return max(japanese, key=lambda enc: (japanese[enc], -_LEGACY_CJK_RERANK_ENCODINGS.index(enc)))

        return max(scores, key=lambda enc: (scores[enc], -_LEGACY_CJK_RERANK_ENCODINGS.index(enc)))

    def _looks_like_utf8_with_few_errors(self, raw: bytes) -> bool:
        """Return True when invalid UTF-8 bytes are likely isolated damage."""
        stats = _utf8_sequence_stats(raw)
        if stats.high_bytes == 0 or stats.invalid_bytes == 0:
            return False
        if stats.valid_multibyte_bytes < 4:
            return False

        valid_high_ratio = stats.valid_multibyte_bytes / stats.high_bytes
        invalid_high_ratio = stats.invalid_bytes / stats.high_bytes
        invalid_total_ratio = stats.invalid_bytes / len(raw)
        if valid_high_ratio < 0.80 or invalid_high_ratio > 0.20:
            return False
        if invalid_total_ratio > 0.05 and stats.invalid_bytes > 8:
            return False

        text = raw.decode("utf-8", errors="replace")
        return is_mostly_text(text, threshold=self._text_quality_threshold)


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _decode_candidate(raw: bytes, enc: str, *, final: bool) -> str:
    """Decode a candidate encoding, optionally allowing a truncated tail."""
    if final:
        return raw.decode(enc)
    decoder_factory = codecs.getincrementaldecoder(enc)
    decoder = decoder_factory(errors="strict")
    return decoder.decode(raw, final=False)


def _candidate_decode_final(enc: str, *, sample_final: bool) -> bool:
    """Return whether candidate decoding should treat the sample as complete."""
    return True if _canonical_encoding(enc) == "utf-8" else sample_final


def _has_only_sparse_trailing_high_bytes(raw: bytes) -> bool:
    """Return True when non-ASCII evidence is only at the sample boundary."""
    high_positions = [i for i, byte in enumerate(raw) if byte >= 0x80]
    return 0 < len(high_positions) <= _DETECT_TAIL_BYTES and high_positions[0] >= len(raw) - _DETECT_TAIL_BYTES


def _legacy_cjk_script_score(text: str, enc: str) -> float:
    """Return a high-confidence script score for legacy CJK codecs."""
    non_ascii = sum(1 for ch in text if ord(ch) > 0x7F)
    if non_ascii < 8:
        return 0.0

    normalized = enc.lower().replace("_", "-")
    if normalized in {"shift-jis", "cp932"}:
        kana = sum(1 for ch in text if _is_japanese_kana(ord(ch)))
        ratio = kana / non_ascii
        if kana < 4 or ratio < 0.15:
            return 0.0
        return 3.0 + ratio + min(kana / 10_000, 0.1)

    if normalized in {"euc-kr", "cp949"}:
        hangul = sum(1 for ch in text if _is_hangul(ord(ch)))
        ratio = hangul / non_ascii
        if hangul < 4 or ratio < 0.65:
            return 0.0
        return 3.0 + ratio + min(hangul / 10_000, 0.1)

    return 0.0


def _is_japanese_kana(cp: int) -> bool:
    return 0x3040 <= cp <= 0x30FF


def _is_hangul(cp: int) -> bool:
    return 0x1100 <= cp <= 0x11FF or 0x3130 <= cp <= 0x318F or 0xAC00 <= cp <= 0xD7AF


def is_mostly_text(s: str, *, threshold: float = 0.05) -> bool:
    """Return True if the string contains few suspicious control characters.

    Args:
        s: Decoded text to check.
        threshold: Maximum ratio of C0 control chars (excl. common whitespace)
            to total length.  Default 5%.
    """
    if not s:
        return True
    bad = 0
    for ch in s:
        cp = ord(ch)
        if ch in "\b\t\n\f\r":
            continue
        if 0 <= cp < 32:
            bad += 1
    return (bad / len(s)) <= threshold


@dataclass(frozen=True, slots=True)
class _Utf8SequenceStats:
    high_bytes: int
    valid_multibyte_bytes: int
    invalid_bytes: int


def _utf8_sequence_stats(raw: bytes) -> _Utf8SequenceStats:
    """Count valid UTF-8 multibyte bytes and malformed high bytes."""
    high_bytes = sum(1 for b in raw if b >= 0x80)
    valid_multibyte_bytes = 0
    invalid_bytes = 0
    i = 0
    size = len(raw)

    while i < size:
        b0 = raw[i]
        if b0 < 0x80:
            i += 1
            continue

        seq_len = _utf8_sequence_length(raw, i)
        if seq_len > 0:
            valid_multibyte_bytes += seq_len
            i += seq_len
        else:
            invalid_bytes += 1
            i += 1

    return _Utf8SequenceStats(
        high_bytes=high_bytes,
        valid_multibyte_bytes=valid_multibyte_bytes,
        invalid_bytes=invalid_bytes,
    )


def _utf8_sequence_length(raw: bytes, index: int) -> int:
    """Return the valid UTF-8 sequence length at *index*, or 0."""
    b0 = raw[index]
    remaining = len(raw) - index
    if 0xC2 <= b0 <= 0xDF:
        return 2 if remaining >= 2 and _is_utf8_continuation(raw[index + 1]) else 0
    if b0 == 0xE0:
        return 3 if remaining >= 3 and 0xA0 <= raw[index + 1] <= 0xBF and _is_utf8_continuation(raw[index + 2]) else 0
    if 0xE1 <= b0 <= 0xEC or 0xEE <= b0 <= 0xEF:
        return (
            3
            if remaining >= 3 and _is_utf8_continuation(raw[index + 1]) and _is_utf8_continuation(raw[index + 2])
            else 0
        )
    if b0 == 0xED:
        return 3 if remaining >= 3 and 0x80 <= raw[index + 1] <= 0x9F and _is_utf8_continuation(raw[index + 2]) else 0
    if b0 == 0xF0:
        return (
            4
            if remaining >= 4
            and 0x90 <= raw[index + 1] <= 0xBF
            and _is_utf8_continuation(raw[index + 2])
            and _is_utf8_continuation(raw[index + 3])
            else 0
        )
    if 0xF1 <= b0 <= 0xF3:
        return (
            4
            if remaining >= 4
            and _is_utf8_continuation(raw[index + 1])
            and _is_utf8_continuation(raw[index + 2])
            and _is_utf8_continuation(raw[index + 3])
            else 0
        )
    if b0 == 0xF4:
        return (
            4
            if remaining >= 4
            and 0x80 <= raw[index + 1] <= 0x8F
            and _is_utf8_continuation(raw[index + 2])
            and _is_utf8_continuation(raw[index + 3])
            else 0
        )
    return 0


def _is_utf8_continuation(b: int) -> bool:
    return 0x80 <= b <= 0xBF


def _canonical_encoding(enc: str | None) -> str | None:
    """Normalize encoding names to canonical form.

    - ``ascii`` / ``utf_8`` → ``utf-8``
    - Underscores → hyphens, lowercased
    """
    if not enc:
        return None
    e = enc.lower().replace("_", "-")
    if e == "ascii":
        return "utf-8"
    return e


def _with_system_ansi_encoding(encodings: tuple[str, ...]) -> tuple[str, ...]:
    """On Windows, append the system ANSI code page encoding if not already present."""
    import sys

    if sys.platform != "win32":
        return encodings

    import locale

    ansi_enc = locale.getpreferredencoding(False)
    if not ansi_enc:
        return encodings

    normalized = _canonical_encoding(ansi_enc) or ansi_enc.lower()
    if any(_canonical_encoding(e) == normalized for e in encodings):
        return encodings

    return (*encodings, normalized)


@functools.cache
def _get_detector() -> EncodingDetector:
    """Lazy-initialized singleton for module-level convenience functions."""
    return EncodingDetector()


def decode_bytes(raw: bytes | bytearray, *, errors: Literal["strict", "replace"] = "replace") -> str:
    """Detect encoding and decode *raw* bytes to str.

    Convenience wrapper around :meth:`EncodingDetector.decode` using a
    module-level singleton. Use ``errors="strict"`` when lossy replacement
    would change configuration values or data that will be written back.
    """
    return _get_detector().decode(raw, errors=errors)
