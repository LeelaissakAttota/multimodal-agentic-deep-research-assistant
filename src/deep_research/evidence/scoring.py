"""Heuristic source credibility and per-claim confidence (no model calls, fully local).

Credibility is a domain heuristic, not a fact check: government, academic and
inter-governmental domains and established journals rank highest; major news
and reference works next; Wikipedia medium; user-generated platforms and blogs
low; unknown domains "unverified". User-attached files are trusted as the
user's own primary material.

Claim confidence scores each cited sentence of a report by how many distinct
sources back it and how credible they are, minus a penalty for hedged wording.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

_GOV = re.compile(r"(^|\.)(gov|mil|edu|int)(\.[a-z]{2})?$|(^|\.)(ac|gov|edu|go|gob|gouv|nic)\.[a-z]{2}$")
_SCIENCE = {
    "nature.com": 0.9, "science.org": 0.9, "sciencedirect.com": 0.85, "springer.com": 0.85, "link.springer.com": 0.85,
    "wiley.com": 0.85, "onlinelibrary.wiley.com": 0.85, "thelancet.com": 0.9, "nejm.org": 0.9, "bmj.com": 0.9,
    "cell.com": 0.85, "plos.org": 0.85, "pnas.org": 0.9, "jamanetwork.com": 0.9, "ieee.org": 0.85,
    "ieeexplore.ieee.org": 0.85, "acm.org": 0.85, "dl.acm.org": 0.85, "jstor.org": 0.85, "tandfonline.com": 0.8,
    "frontiersin.org": 0.75, "mdpi.com": 0.7, "arxiv.org": 0.7, "biorxiv.org": 0.7, "medrxiv.org": 0.7,
    "ssrn.com": 0.7, "researchgate.net": 0.6, "semanticscholar.org": 0.75, "doi.org": 0.8,
    "who.int": 0.95, "un.org": 0.9, "oecd.org": 0.9, "worldbank.org": 0.9, "imf.org": 0.9, "ipcc.ch": 0.95,
    "europa.eu": 0.9, "noaa.gov": 0.95, "nasa.gov": 0.95, "nih.gov": 0.95, "cdc.gov": 0.95,
}
_NEWS = {
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "nytimes.com", "theguardian.com", "economist.com", "ft.com",
    "wsj.com", "washingtonpost.com", "npr.org", "bloomberg.com", "nationalgeographic.com", "scientificamerican.com",
    "newscientist.com", "theatlantic.com", "aljazeera.com", "thehindu.com", "indianexpress.com", "britannica.com",
    "smithsonianmag.com", "pewresearch.org", "statista.com", "ourworldindata.org", "carbonbrief.org",
}
_LOW = {
    "medium.com", "substack.com", "blogspot.com", "wordpress.com", "tumblr.com", "reddit.com", "quora.com",
    "youtube.com", "tiktok.com", "facebook.com", "instagram.com", "x.com", "twitter.com", "pinterest.com",
    "linkedin.com", "answers.com", "fandom.com", "weebly.com", "wixsite.com",
}
_HEDGE = re.compile(r"\b(may|might|could|possibly|perhaps|suggests?|unclear|uncertain|estimated|reportedly|"
                    r"likely|unlikely|appears?|seems?|approximately|roughly|preliminary)\b", re.I)
_CITE = re.compile(r"\[(\d+(?:\s*[,\u2013-]\s*\d+)*)\]")
_SKIP_SECTIONS = ("sources", "attached files", "references")


def label_for(score: float) -> str:
    return "High" if score >= 0.75 else "Medium" if score >= 0.55 else "Unverified" if score >= 0.4 else "Low"


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _match(host: str, table: Any) -> str | None:
    parts = host.split(".")
    for i in range(len(parts) - 1):
        cand = ".".join(parts[i:])
        if cand in table:
            return cand
    return None


def credibility(url: str | None, *, kind: str | None = None) -> dict[str, Any]:  # noqa: C901 - flat rule table
    """Score one source 0-1 with a label and a short human reason."""

    if not url:
        if kind == "attachment":
            return {"score": 0.85, "label": "Provided", "reason": "user-attached file (primary material)"}
        return {"score": 0.4, "label": "Unverified", "reason": "no URL"}
    host = _host(str(url))
    if not host:
        return {"score": 0.3, "label": "Low", "reason": "unparseable URL"}
    try:
        ipaddress.ip_address(host)
        return {"score": 0.3, "label": "Low", "reason": "bare IP address"}
    except ValueError:
        pass
    if (hit := _match(host, _SCIENCE)) is not None:
        score, reason = _SCIENCE[hit], "preprint server" if hit.endswith("rxiv.org") or hit == "ssrn.com" else \
            "journal / research publisher" if hit not in ("who.int", "un.org", "oecd.org", "worldbank.org", "imf.org",
                                                          "ipcc.ch", "europa.eu") and not hit.endswith(".gov") \
            else "official / inter-governmental body"
    elif _GOV.search(host):
        score, reason = 0.9, "government / academic domain"
    elif host.endswith("wikipedia.org"):
        score, reason = 0.6, "encyclopedia (community-edited)"
    elif _match(host, _NEWS) is not None:
        score, reason = 0.78, "established news / reference publisher"
    elif _match(host, _LOW) is not None:
        score, reason = 0.3, "user-generated / blog platform"
    elif host.endswith(".org"):
        score, reason = 0.55, "organisation site (unrated)"
    else:
        score, reason = 0.45, "unrated domain"
    if str(url).lower().startswith("http://"):
        score -= 0.05
        reason += "; not HTTPS"
    score = round(max(0.05, min(0.98, score)), 2)
    return {"score": score, "label": label_for(score), "reason": reason}


def score_citations(citations: list[dict[str, Any]]) -> None:
    """Attach ``credibility`` to each citation dict in place."""

    for c in citations:
        if isinstance(c, dict):
            c["credibility"] = credibility(c.get("url"), kind=c.get("kind"))


def _numbers(group: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"\s*,\s*", group):
        bounds = re.split(r"\s*[\u2013-]\s*", part)
        if len(bounds) == 2 and bounds[0].isdigit() and bounds[1].isdigit() and int(bounds[1]) - int(bounds[0]) < 20:
            out.extend(range(int(bounds[0]), int(bounds[1]) + 1))
        elif part.isdigit():
            out.append(int(part))
    return out


def _statements(content: str) -> list[tuple[str, str]]:
    """(section, statement) pairs: sentences of prose, whole rows of tables."""

    section, out = "", []
    for raw in content.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            section = line.lstrip("#").strip()
            continue
        if not line or section.lower() in _SKIP_SECTIONS or re.fullmatch(r"\|?[\s:|-]+\|?", line):
            continue
        if line.startswith("|"):
            out.append((section, " | ".join(c.strip() for c in line.strip("|").split("|") if c.strip())))
            continue
        line = re.sub(r"^([-*+]|\d+\.)\s+", "", line)
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z\"(])|(?<=\])\s+(?=[A-Z])", line):
            out.append((section, sentence.strip()))
    return out


_META = re.compile(r"^\W*(sources?|the sources?|citations?|search results?)\b", re.I)


def clean_claim_text(text: str) -> str:
    """Claim text for display: citation markers and the empty separators they leave behind removed."""

    t = _CITE.sub("\x00", re.sub(r"\*\*|__", "", str(text)))
    t = re.sub(r"\s*\x00(?:(?:\s*(?:,|and\b|&))*\s*\x00)*", "", t)
    t = re.sub(r"\(\s*[,;]?\s*\)", "", t)
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    t = re.sub(r"[\s|,;]+$", "", t).strip()
    return re.sub(r"\s{2,}", " ", t)


def claim_scores(content: str, citations: list[dict[str, Any]], *, limit: int = 60) -> list[dict[str, Any]]:
    """Per-claim confidence for every cited statement in the report content."""

    by_number = {int(c["number"]): c for c in citations if isinstance(c, dict) and str(c.get("number", "")).isdigit()}
    claims: list[dict[str, Any]] = []
    for section, statement in _statements(content):
        nums = [n for g in _CITE.findall(statement) for n in _numbers(g) if n in by_number]
        nums = list(dict.fromkeys(nums))
        if not nums or _META.match(statement):
            continue
        creds = [float((by_number[n].get("credibility") or credibility(by_number[n].get("url"),
                                                                       kind=by_number[n].get("kind")))["score"])
                 for n in nums]
        mean = sum(creds) / len(creds)
        conf = 0.3 + 0.45 * mean + 0.08 * (min(len(nums), 3) - 1)
        reasons = [f"{len(nums)} source{'s' if len(nums) > 1 else ''}", f"avg credibility {mean:.2f}"]
        if _HEDGE.search(_CITE.sub("", statement)):
            conf -= 0.1
            reasons.append("hedged wording")
        conf = round(max(0.05, min(0.95, conf)), 2)
        claims.append({"text": re.sub(r"\*\*|__", "", statement), "section": section, "citations": nums,
                       "confidence": conf, "label": label_for(conf) if conf >= 0.55 else "Low",
                       "reason": ", ".join(reasons)})
        if len(claims) >= limit:
            break
    return claims


def apply_scores(report: dict[str, Any]) -> dict[str, Any]:
    """Add credibility to citations and ``claim_scores`` to a report (idempotent; works on saved runs)."""

    citations = report.get("citations") or report.get("sources") or []
    if isinstance(citations, list):
        score_citations(citations)
        if "claim_scores" not in report:
            report["claim_scores"] = claim_scores(str(report.get("content") or ""), citations)
    return report
