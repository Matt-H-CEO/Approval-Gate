"""Text normalization shared by all persona checkers: number words, money, percents, durations.

Nothing here knows about any persona's data. Standard library only.
"""
import re

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_WORD = r"(?:" + "|".join(sorted(list(_UNITS) + list(_TENS) + ["hundred", "thousand"], key=len, reverse=True)) + r")"
_NUMBER_PHRASE = re.compile(rf"\b{_WORD}(?:[\s-]+(?:and[\s-]+)?{_WORD})*\b", re.I)


def _phrase_value(phrase):
    total, current = 0, 0
    for tok in re.split(r"[\s-]+", phrase.lower()):
        if tok == "and" or not tok:
            continue
        if tok in _UNITS:
            current += _UNITS[tok]
        elif tok in _TENS:
            current += _TENS[tok]
        elif tok == "hundred":
            current = (current or 1) * 100
        elif tok == "thousand":
            total += (current or 1) * 1000
            current = 0
    return total + current


def words_to_digits(text):
    """'nineteen dollars' -> '19 dollars'; 'forty-nine' -> '49'. Leaves everything else unchanged."""
    return _NUMBER_PHRASE.sub(lambda m: str(_phrase_value(m.group(0))), text)


def normalize(text):
    t = words_to_digits(text)
    t = t.replace("’", "'").replace("–", "-").replace("—", "-")
    return t


def sentences(text):
    """Split into sentences without breaking decimals like 39.20 or $49.00."""
    parts = re.split(r"(?<=[.!?;])\s+(?=[A-Z0-9$\"'(])", text.strip())
    return [p for p in parts if p.strip()]


def to_float(raw):
    return float(raw.replace(",", ""))


_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mm": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9}
_MONEY = re.compile(
    r"(?:\$\s?|\busd\s?)(?P<a>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?:\s?(?P<s>thousand|million|billion|bn|mm|k|m|b)\b)?"
    r"|(?P<b>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?(?P<s2>thousand|million|billion)?\s?(?:dollars|usd|bucks)\b",
    re.I,
)
_PER_MONTH = re.compile(r"^\s*(?:/\s?|per\s|a\s|an\s|each\s|every\s)?\s*(?:mo\b|mos\b|month\b|monthly\b)", re.I)
_PER_YEAR = re.compile(r"^\s*(?:/\s?|per\s|a\s|an\s|each\s|every\s)?\s*(?:yr\b|year\b|annum\b|annually\b|yearly\b)", re.I)


def money(text):
    """Dollar amounts with their position and stated period ('month', 'year' or '')."""
    out = []
    for m in _MONEY.finditer(text):
        raw = m.group("a") or m.group("b")
        scale = (m.group("s") or m.group("s2") or "").lower()
        value = to_float(raw) * _SCALE.get(scale, 1)
        after = text[m.end():m.end() + 30]
        period = "month" if _PER_MONTH.search(after) else "year" if _PER_YEAR.search(after) else ""
        out.append({"value": value, "start": m.start(), "end": m.end(), "text": m.group(0).strip(), "period": period})
    return out


_PERCENT = re.compile(r"(?P<v>\d+(?:\.\d+)?)\s?(?:%|percent\b|per cent\b)", re.I)


def percents(text):
    return [{"value": float(m.group("v")), "start": m.start(), "end": m.end(), "text": m.group(0)}
            for m in _PERCENT.finditer(text)]


_DURATION = re.compile(
    r"(?P<n>\d+(?:\.\d+)?|an?|one)\s?(?:-\s?)?(?:full\s|calendar\s|business\s|working\s)?"
    r"(?P<u>hours?|hrs?|days?|weeks?|months?|years?)\b",
    re.I,
)
_TO_HOURS = {"hour": 1, "hr": 1, "day": 24, "week": 168, "month": 720, "year": 8760}
_TO_DAYS = {"hour": 1 / 24, "hr": 1 / 24, "day": 1, "week": 7, "month": 30, "year": 365}


def durations(text):
    """Durations with value in hours and in days. 'a day' -> 24 h; 'two weeks' (after words_to_digits) -> 14 d."""
    out = []
    for m in _DURATION.finditer(text):
        n = m.group("n").lower()
        n = 1.0 if n in ("a", "an", "one") else float(n)
        unit = m.group("u").lower().rstrip("s")
        out.append({"hours": n * _TO_HOURS[unit], "days": n * _TO_DAYS[unit], "unit": unit,
                    "start": m.start(), "end": m.end(), "text": m.group(0)})
    return out


def close(a, b, rel=0.005, abs_tol=0.011):
    """Equal within normal rounding: 0.5% relative or about a cent."""
    return abs(a - b) <= max(abs_tol, rel * max(abs(a), abs(b)))


def fmt_money(v):
    return f"${v:,.2f}".replace(".00", "")
