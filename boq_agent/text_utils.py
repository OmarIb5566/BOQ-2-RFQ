"""Arabic/English text helpers: normalisation, number parsing, keyword matching."""
from __future__ import annotations

import datetime as _dt
import re
import unicodedata

_AR_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_ARABIC = re.compile(r"[؀-ۿ]")
_LATIN = re.compile(r"[A-Za-z]")
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def nfkc(s) -> str:
    """NFKC turns Arabic presentation forms (common in PDFs) back into base letters."""
    return unicodedata.normalize("NFKC", str(s)) if s is not None else ""


def norm(s) -> str:
    """Normalise text for matching (not for display)."""
    s = nfkc(s)
    s = _AR_DIACRITICS.sub("", s)
    s = re.sub("[إأآٱ]", "ا", s)
    s = s.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    s = s.translate(_AR_DIGITS).lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def is_arabic(s: str) -> bool:
    return len(_ARABIC.findall(s or "")) > len(_LATIN.findall(s or ""))


def has_text(s: str) -> bool:
    """True if the string contains letters (not just numbers / punctuation)."""
    return bool(_ARABIC.search(s or "") or _LATIN.search(s or ""))


def parse_number(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = nfkc(v).translate(_AR_DIGITS).replace("٫", ".").replace("٬", ",")
    s = s.replace(",", "").replace(" ", "").strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return float(s)
    return None


def fmt_value(v) -> str:
    """Display value for a spreadsheet cell."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (int, float)):
        if abs(v - round(v)) < 1e-9:
            return f"{int(round(v)):,}"
        return f"{v:,.3f}".rstrip("0").rstrip(".")
    return nfkc(v).strip()


def keyword_hits(text_norm: str, keywords_norm: list[str]) -> list[str]:
    """Keywords found in already-normalised text. Short keywords need word boundaries."""
    hits = []
    for kw in keywords_norm:
        if not kw:
            continue
        if len(kw) <= 3:
            if re.search(rf"(^|[^\w]){re.escape(kw)}($|[^\w])", text_norm):
                hits.append(kw)
        elif kw in text_norm:
            hits.append(kw)
    return hits


# "the item includes formwork, movement joints, …" lists ancillary scope, not the item's trade.
# Applied to norm()-ed text; a clause runs to the next sentence / sub-item separator.
_INCLUSION_RE = re.compile(
    r"(\b(the\s+)?(item|rate|price|unit\s*rate)s?\s+(shall\s+)?(also\s+)?includ\w*|\bincluding\b|\bincludes?\b|"
    r"\binclusive\s+of\b|\bcomplete\s+with\b|\bas\s+well\s+as\b|"
    r"شاملا|شامله|شامل|ويشمل|يشمل|وتشمل|تشمل|بما في ذلك|متضمنا|ويتضمن|يتضمن|مع جميع|وجميع)"
    r"[^.;؛—\n]*")


def strip_inclusions(text_norm: str) -> str:
    """Primary scope of a description: drops 'includes … / شاملا …' clauses."""
    return _INCLUSION_RE.sub(" ", text_norm)


def truncate(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def safe_filename(s: str, max_len: int = 80) -> str:
    s = re.sub(r'[<>:"/\\|?*\n\r\t]+', " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:max_len] or "untitled"
