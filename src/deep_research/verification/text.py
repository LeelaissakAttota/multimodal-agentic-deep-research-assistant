"""Small text utilities shared by the verification heuristics (no external dependencies)."""

from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9][a-z0-9\-]*")
_NUMBER = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)*)(\s*%|\s*percent)?")
_CITE = re.compile(r"\[(\d+(?:\s*[,\u2013-]\s*\d+)*)\]")

STOPWORDS = frozenset(
    "a an and are as at be been being but by can could did do does for from had has have he her his how i if in "
    "into is it its may might more most much of on or our over she should so some such than that the their them "
    "then there these they this those through to under up was we were what when where which while who whose why "
    "will with would you your also about after against all any because before between both each few many other "
    "only own same very via per across among within without upon however".split()
)
NEGATIONS = frozenset({"not", "no", "never", "none", "cannot", "can't", "won't", "isn't", "aren't", "doesn't",
                       "don't", "didn't", "wasn't", "weren't", "neither", "nor", "without", "lack", "lacks",
                       "fails", "failed", "unlikely"})


def tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("\u2019", "'"))


def content_terms(text: str) -> set[str]:
    """Lower-cased content words (stopwords, citation numbers and 1-2 char tokens removed), lightly stemmed."""
    return {stem(t) for t in tokens(strip_citations(text)) if len(t) > 2 and t not in STOPWORDS and not t.isdigit()}


def stem(word: str) -> str:
    for suffix in ("ies", "es", "s", "ing", "ed", "ly"):
        if len(word) > len(suffix) + 3 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def numbers(text: str) -> set[str]:
    """Numeric figures mentioned in the text (citation markers excluded), normalised (1,000 -> 1000)."""
    out: set[str] = set()
    for match in _NUMBER.finditer(strip_citations(text)):
        value = match.group(1).replace(",", "")
        out.add(value + ("%" if match.group(2) else ""))
    return out


def negated(text: str) -> bool:
    words = re.findall(r"[a-z']+", text.lower().replace("\u2019", "'"))
    return any(w in NEGATIONS or w.endswith("n't") for w in words)


def citation_numbers(text: str) -> list[int]:
    found: list[int] = []
    for group in _CITE.findall(text):
        for n in re.findall(r"\d+", group):
            if int(n) not in found:
                found.append(int(n))
    return found


def strip_citations(text: str) -> str:
    return _CITE.sub("", text)


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0
