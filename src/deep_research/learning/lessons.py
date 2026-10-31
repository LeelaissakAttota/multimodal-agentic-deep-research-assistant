"""Local SQLite store for learned lessons, tool reliability stats, run scores and identity versions.

Safety rules applied on every write:
- secrets and personal data are rejected (API keys, tokens, emails, phone or card numbers);
- near-duplicates reinforce the existing lesson instead of adding a new row;
- a lesson that contradicts an existing one (same subject, opposite direction) lowers the old one;
- confidence decays with age when unused; low-confidence lessons are pruned; the store is capped.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from deep_research.observability.redaction import REDACTED, redact_text

MAX_LESSON_CHARS = 220
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset("a an the and or of for to in on with is are be by at as from this that it its use when "
                  "prefer avoid more less run runs query queries".split())
_PII = (
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),  # email
    re.compile(r"(?<!\d)(?:\+?\d[\s-]?){10,15}(?!\d)"),  # phone / card-like digit runs
    re.compile(r"(?i)\b(password|passwd|api[_ -]?key|secret|token)\b"),
)
_NEGATIVE = re.compile(r"(?i)\b(avoid|skip|lower|deprioriti[sz]e|do not|don't|never|stop)\b")
_POSITIVE = re.compile(r"(?i)\b(prefer|use|raise|priorit[iy][sz]?e?|always|add)\b")


def tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1}


def is_unsafe(text: str) -> bool:
    """True when the text holds a secret or personal data and must never be stored."""
    return redact_text(text) != text or REDACTED in text or any(p.search(text) for p in _PII)


def _polarity(text: str) -> int:
    return -1 if _NEGATIVE.search(text) else 1 if _POSITIVE.search(text) else 0


def _similar(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


@dataclass
class Lesson:
    id: str
    text: str
    topic: str
    evidence: str
    run_id: str
    created_at: float
    confidence: float
    usage_count: int = 0
    enabled: bool = True
    source: str = "deterministic"
    last_used_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (id TEXT PRIMARY KEY, text TEXT NOT NULL, topic TEXT NOT NULL, evidence TEXT,
  run_id TEXT, created_at REAL, confidence REAL, usage_count INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1,
  source TEXT, last_used_at REAL);
CREATE TABLE IF NOT EXISTS tool_stats (tool TEXT PRIMARY KEY, calls INTEGER, successes INTEGER,
  total_ms REAL, last_error TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, created_at REAL, objective_chars INTEGER, topic TEXT,
  score REAL, lessons_applied INTEGER, metrics TEXT);
CREATE TABLE IF NOT EXISTS identity_versions (version TEXT, core_hash TEXT, first_seen REAL,
  PRIMARY KEY (version, core_hash));
"""


class LessonStore:
    def __init__(self, path: str | Path, *, max_lessons: int = 50, min_confidence: float = 0.2,
                 half_life_days: float = 30.0) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_lessons = max_lessons
        self.min_confidence = min_confidence
        self.half_life_days = half_life_days
        self._lock = threading.Lock()
        with self._conn() as db:
            db.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    # ----- lessons -----------------------------------------------------------------------------
    def add(self, text: str, *, topic: str = "general", evidence: str = "", run_id: str = "",
            confidence: float = 0.6, source: str = "deterministic", now: float | None = None) -> Lesson | None:
        """Validate and store one lesson. Returns the stored/reinforced lesson, or ``None`` if rejected."""
        text = " ".join(str(text).split())[:MAX_LESSON_CHARS]
        evidence = " ".join(str(evidence).split())[:400]
        if len(text) < 12 or is_unsafe(text) or is_unsafe(evidence):
            return None
        now = now or time.time()
        confidence = min(max(float(confidence), 0.05), 0.95)
        new_tokens = tokens(text)
        with self._lock, self._conn() as db:
            for row in db.execute("SELECT * FROM lessons WHERE topic IN (?, 'general')", (topic,)).fetchall():
                old_tokens = tokens(row["text"])
                sim = _similar(new_tokens, old_tokens)
                if sim >= 0.6 and _polarity(row["text"]) == _polarity(text):
                    conf = min(0.95, row["confidence"] + 0.1)
                    db.execute("UPDATE lessons SET confidence=?, evidence=?, run_id=? WHERE id=?",
                               (conf, evidence or row["evidence"], run_id or row["run_id"], row["id"]))
                    return self._get(db, row["id"])
                if sim >= 0.5 and _polarity(row["text"]) * _polarity(text) < 0:
                    db.execute("UPDATE lessons SET confidence=confidence-0.25 WHERE id=?", (row["id"],))
            lesson = Lesson(id=uuid.uuid4().hex[:12], text=text, topic=topic, evidence=evidence, run_id=run_id,
                            created_at=now, confidence=confidence, source=source)
            db.execute("INSERT INTO lessons VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (lesson.id, text, topic, evidence, run_id, now, confidence, 0, 1, source, None))
        self.prune(now=now)
        return lesson

    @staticmethod
    def _row(row: sqlite3.Row) -> Lesson:
        return Lesson(**{**dict(row), "enabled": bool(row["enabled"])})

    def _get(self, db: sqlite3.Connection, lesson_id: str) -> Lesson | None:
        row = db.execute("SELECT * FROM lessons WHERE id=?", (lesson_id,)).fetchone()
        return self._row(row) if row else None

    def all_lessons(self, *, include_disabled: bool = True) -> list[Lesson]:
        sql = "SELECT * FROM lessons" + ("" if include_disabled else " WHERE enabled=1")
        with self._conn() as db:
            rows = db.execute(sql + " ORDER BY confidence DESC, created_at DESC").fetchall()
        return [self._row(r) for r in rows]

    def set_enabled(self, lesson_id: str, enabled: bool) -> None:
        with self._lock, self._conn() as db:
            db.execute("UPDATE lessons SET enabled=? WHERE id=?", (int(enabled), lesson_id))

    def delete(self, lesson_id: str) -> None:
        with self._lock, self._conn() as db:
            db.execute("DELETE FROM lessons WHERE id=?", (lesson_id,))

    def effective_confidence(self, lesson: Lesson, now: float | None = None) -> float:
        """Confidence decays by half every ``half_life_days`` since the lesson was last created/used."""
        age_days = ((now or time.time()) - (lesson.last_used_at or lesson.created_at)) / 86_400
        return lesson.confidence * math.pow(0.5, max(age_days, 0) / self.half_life_days)

    def prune(self, now: float | None = None) -> int:
        """Drop decayed/contradicted lessons below ``min_confidence`` and cap the store size."""
        lessons = self.all_lessons()
        drop = [x.id for x in lessons if self.effective_confidence(x, now) < self.min_confidence]
        keep = [x for x in lessons if x.id not in drop]
        keep.sort(key=lambda x: (self.effective_confidence(x, now), x.usage_count), reverse=True)
        drop += [x.id for x in keep[self.max_lessons:]]
        if drop:
            with self._lock, self._conn() as db:
                db.executemany("DELETE FROM lessons WHERE id=?", [(i,) for i in drop])
        return len(drop)

    def retrieve(self, query: str, topic: str, k: int = 3, *,
                 embed: Callable[[Sequence[str]], list[list[float]]] | None = None,
                 now: float | None = None) -> list[Lesson]:
        """Top-k enabled lessons for a query: keyword overlap (+ cosine when ``embed`` is given),
        boosted for the same topic, weighted by decayed confidence."""
        lessons = self.all_lessons(include_disabled=False)
        if not lessons or k < 1:
            return []
        q = tokens(query)
        sims = [0.0] * len(lessons)
        if embed is not None:
            try:
                vectors = embed([query, *[x.text for x in lessons]])
                sims = [sum(a * b for a, b in zip(vectors[0], v)) for v in vectors[1:]]
            except Exception:
                sims = [0.0] * len(lessons)
        scored = []
        for lesson, sim in zip(lessons, sims):
            overlap = len(q & tokens(lesson.text + " " + lesson.evidence)) / (len(q) or 1)
            relevance = overlap + max(sim, 0.0) + (0.6 if lesson.topic == topic else 0.2 if lesson.topic == "general" else 0)
            if relevance <= 0.2:
                continue
            scored.append((relevance * self.effective_confidence(lesson, now), lesson))
        scored.sort(key=lambda s: s[0], reverse=True)
        return [lesson for _, lesson in scored[:k]]

    def mark_used(self, ids: Iterable[str], now: float | None = None) -> None:
        with self._lock, self._conn() as db:
            db.executemany("UPDATE lessons SET usage_count=usage_count+1, last_used_at=? WHERE id=?",
                           [(now or time.time(), i) for i in ids])

    # ----- tool reliability --------------------------------------------------------------------
    def record_tool(self, tool: str, success: bool, latency_ms: float, error: str | None = None) -> None:
        err = redact_text(error or "")[:200] or None
        with self._lock, self._conn() as db:
            db.execute(
                "INSERT INTO tool_stats VALUES (?,1,?,?,?,?) ON CONFLICT(tool) DO UPDATE SET calls=calls+1, "
                "successes=successes+excluded.successes, total_ms=total_ms+excluded.total_ms, "
                "last_error=COALESCE(excluded.last_error, last_error), updated_at=excluded.updated_at",
                (tool, int(success), max(latency_ms, 0.0), err, time.time()))

    def tool_stats(self) -> list[dict[str, Any]]:
        with self._conn() as db:
            rows = db.execute("SELECT * FROM tool_stats ORDER BY tool").fetchall()
        return [{"tool": r["tool"], "calls": r["calls"], "success_rate": r["successes"] / r["calls"] if r["calls"] else 0.0,
                 "avg_latency_ms": r["total_ms"] / r["calls"] if r["calls"] else 0.0, "last_error": r["last_error"]}
                for r in rows]

    def rank_tools(self, tools: Sequence[str], *, min_calls: int = 3, skip_below: float = 0.25) -> tuple[list[str], list[str]]:
        """Order tools by reliability (Laplace-smoothed success rate, then latency); skip proven-broken ones.

        Tools with fewer than ``min_calls`` observations keep a neutral prior and are never skipped.
        """
        stats = {s["tool"]: s for s in self.tool_stats()}

        def score(tool: str) -> tuple[float, float]:
            s = stats.get(tool)
            if not s:
                return (0.5, 0.0)
            ok = s["success_rate"] * s["calls"]
            return ((ok + 1) / (s["calls"] + 2), -s["avg_latency_ms"])

        skipped = [t for t in tools if (s := stats.get(t)) and s["calls"] >= min_calls and s["success_rate"] < skip_below]
        kept = [t for t in tools if t not in skipped]
        order = sorted(kept, key=lambda t: (-score(t)[0], -score(t)[1], list(tools).index(t)))
        return order, skipped

    # ----- runs + identity ---------------------------------------------------------------------
    def record_run(self, run_id: str, *, topic: str, score: float, lessons_applied: int,
                   metrics: dict[str, Any], objective_chars: int = 0) -> None:
        with self._lock, self._conn() as db:
            db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?)",
                       (run_id, time.time(), objective_chars, topic, score, lessons_applied, json.dumps(metrics)))

    def runs(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._conn() as db:
            rows = db.execute("SELECT * FROM runs ORDER BY created_at LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "metrics": json.loads(r["metrics"] or "{}")} for r in rows]

    def note_identity(self, version: str, core_hash: str) -> list[dict[str, Any]]:
        """Record the identity version seen at run time; returns the identity version history."""
        with self._lock, self._conn() as db:
            db.execute("INSERT OR IGNORE INTO identity_versions VALUES (?,?,?)", (version, core_hash, time.time()))
            rows = db.execute("SELECT * FROM identity_versions ORDER BY first_seen").fetchall()
        return [dict(r) for r in rows]
