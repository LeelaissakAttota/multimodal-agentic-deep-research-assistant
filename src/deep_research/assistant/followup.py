"""Follow-up Q&A about one finished report, grounded only in that report.

The answer may only use the report's own text, claims and cited source excerpts.
Every answer carries ``[n]`` citations that exist in the report; when the report
does not support an answer the reply is a refusal flagged ``supported=False``.

With a free model (OpenRouter via the free-model guard) the model phrases the
answer from the retrieved passages and its citations are validated; without a
model (or on any model failure) an extractive answer is built offline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from deep_research.core.agents.llm_support import AgentDecisionError, ask_json, clean_text
from deep_research.models.model_gateway import ModelGateway

_CITE = re.compile(r"\[(\d+(?:\s*[,\u2013-]\s*\d+)*)\]")
_WORD = re.compile(r"[a-z0-9][a-z0-9+#.-]*")
_STOP = frozenset(
    "a an and are as at be by can could did do does for from has have how i in is it its of on or that the this "
    "to was were what when where which who why will with would about into than then there these those they their "
    "report say says said tell me please you your does do any some more most much many also".split()
)
REFUSAL = "The report does not contain evidence that answers this question, so I can't answer it from this report."
MIN_SCORE = 0.34


@dataclass
class Passage:
    text: str
    citations: list[int]
    kind: str  # "report" | "claim" | "source"


@dataclass
class Answer:
    text: str
    citations: list[int]
    supported: bool
    mode: str  # "llm" | "extractive" | "refused"
    passages: list[Passage] = field(default_factory=list)
    reason: str = ""

    def as_meta(self) -> dict[str, Any]:
        return {"citations": self.citations, "supported": self.supported, "mode": self.mode, "reason": self.reason}


def terms(text: str) -> set[str]:
    out = set()
    for w in _WORD.findall(text.lower()):
        w = w.strip(".-")
        if len(w) > 1 and w not in _STOP:
            out.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return out


def _numbers(marker: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"\s*,\s*", marker):
        bounds = [int(x) for x in re.split(r"\s*[\u2013-]\s*", part) if x.strip().isdigit()]
        if len(bounds) == 2 and 0 < bounds[1] - bounds[0] < 20:
            out.extend(range(bounds[0], bounds[1] + 1))
        else:
            out.extend(bounds)
    return out


def _body(content: str) -> str:
    """Report markdown without the trailing source list and generated meta sections."""
    cut = re.split(r"(?im)^#{1,3}\s*(sources|references)\s*$", content)
    return cut[0]


def report_passages(report: dict[str, Any]) -> list[Passage]:
    """Sentences of the report body, scored claims and cited source excerpts."""
    valid = {int(c["number"]) for c in report.get("citations") or [] if isinstance(c, dict) and str(c.get("number", "")).isdigit()}
    out: list[Passage] = []
    for line in _body(str(report.get("content") or "")).splitlines():
        line = line.strip().lstrip("-*• ").strip()
        if not line or line.startswith(("#", "|", "!")):
            continue
        for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line):
            cites = [n for m in _CITE.findall(sent) for n in _numbers(m) if n in valid]
            if len(terms(sent)) >= 3:
                out.append(Passage(sent.strip(), sorted(set(cites)), "report"))
    for claim in report.get("claim_scores") or []:
        if isinstance(claim, dict) and claim.get("text"):
            cites = [int(n) for n in claim.get("citations") or [] if str(n).isdigit() and int(n) in valid]
            out.append(Passage(str(claim["text"]), cites, "claim"))
    for cit in report.get("citations") or []:
        if isinstance(cit, dict) and str(cit.get("number", "")).isdigit():
            text = " ".join(str(cit.get(k) or "") for k in ("title", "excerpt")).strip()
            if text:
                out.append(Passage(text[:1200], [int(cit["number"])], "source"))
    return out


def retrieve(question: str, passages: list[Passage], k: int = 4) -> list[tuple[float, Passage]]:
    """Rank passages by the share of question terms they contain."""
    q = terms(question)
    if not q:
        return []
    scored = []
    for p in passages:
        overlap = len(q & terms(p.text))
        if overlap:
            scored.append((overlap / len(q) + (0.05 if p.citations else 0.0), p))
    scored.sort(key=lambda sp: sp[0], reverse=True)
    return scored[:k]


def extractive_answer(question: str, report: dict[str, Any]) -> Answer:
    """Offline answer: the best-supported cited sentences from the report, or a refusal."""
    ranked = retrieve(question, report_passages(report))
    good = [p for s, p in ranked if s >= MIN_SCORE and p.citations]
    if not good:
        return Answer(REFUSAL, [], False, "refused", [p for _, p in ranked], reason="no supporting evidence in report")
    picked = good[:2]
    lines = []
    for p in picked:
        text = _CITE.sub("", p.text).strip()
        lines.append(f"{text} {''.join(f'[{n}]' for n in p.citations)}")
    cites = sorted({n for p in picked for n in p.citations})
    return Answer("According to the report: " + " ".join(lines), cites, True, "extractive", picked)


def _prompt(question: str, passages: list[Passage]) -> str:
    numbered = "\n".join(f"- ({', '.join(f'[{n}]' for n in p.citations)}) {_CITE.sub('', p.text)[:600]}" for p in passages)
    return (
        "You answer follow-up questions about a research report. Use ONLY the passages below; they are the "
        "report's evidence. Cite with the bracket numbers shown. If the passages do not answer the question, "
        "set supported to false.\n"
        f"Passages:\n{numbered}\n\nQuestion: {question}\n"
        'Reply with JSON only: {"supported": true|false, "answer": "<2-4 sentences with [n] citations>", '
        '"citations": [<numbers>]}'
    )


async def answer_question(question: str, report: dict[str, Any], gateway: ModelGateway | None = None, *,
                          timeout_seconds: float = 30.0) -> Answer:
    """Grounded answer; the model is optional and its citations must come from the retrieved passages."""
    base = extractive_answer(question, report)
    if gateway is None or not base.supported:
        return base
    allowed = {n for p in base.passages for n in p.citations}
    try:
        reply, _ = await ask_json(gateway, _prompt(question, base.passages), max_tokens=400, timeout_seconds=timeout_seconds)
    except AgentDecisionError as exc:
        base.reason = f"model fallback: {exc.reason}"
        return base
    text = clean_text(reply.get("answer"), max_len=1500)
    cites = sorted({int(n) for n in reply.get("citations") or [] if isinstance(n, int) or str(n).isdigit()})
    cites = sorted(set(cites) | {n for m in _CITE.findall(text) for n in _numbers(m)})
    if reply.get("supported") is False:
        return Answer(REFUSAL, [], False, "refused", base.passages, reason="model found no support")
    if not text or not cites or not set(cites) <= allowed:
        base.reason = "model fallback: uncited or out-of-report citations"
        return base
    return Answer(text, cites, True, "llm", base.passages)


def build_chat_gateway(settings: Any) -> ModelGateway | None:
    """Free-model-guarded text gateway, or ``None`` (offline) when no OpenRouter key is configured."""
    key = getattr(settings, "openrouter_api_key", None)
    if not (key and key.get_secret_value().strip()):
        return None
    from deep_research.core.free_model_guard import ModelCapability
    from deep_research.core.provider_wiring import build_provider_gateway

    return build_provider_gateway(settings, capability=ModelCapability.TEXT_GENERATION)


async def ask_report(store: Any, run_id: str, question: str, report: dict[str, Any],
                     gateway: ModelGateway | None = None) -> Answer:
    """Answer and record both turns in the report's chat history (``AssistantStore``)."""
    question = " ".join(question.split())[:1000]
    if not question:
        raise ValueError("question is empty")
    answer = await answer_question(question, report, gateway)
    store.add_chat(run_id, "user", question)
    store.add_chat(run_id, "assistant", answer.text, answer.as_meta())
    return answer
