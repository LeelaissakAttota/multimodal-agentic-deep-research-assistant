"""User profile and research preferences (saved topics, preferred/blocked sources, default depth).

The profile lives in the assistant SQLite store and is applied to the per-run settings:
blocked source tools are disabled, preferred ones are always consulted by the planner,
blocked domains are dropped from results, and the depth sets iterations and result counts.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from deep_research.assistant.store import AssistantStore
from deep_research.core.config import Settings
from deep_research.security.domains import normalize_domain
from deep_research.tools.registry import SOURCE_TOOLS

PROFILE_KEY = "profile"
Depth = Literal["quick", "standard", "deep"]
#: depth -> (max_research_iterations, live_max_search_results, live_max_fetch_pages)
DEPTHS: dict[str, tuple[int, int, int]] = {"quick": (1, 3, 1), "standard": (3, 5, 2), "deep": (5, 8, 4)}
_DOMAIN = re.compile(r"^(?=.{3,253}$)([a-z0-9-]{1,63}\.)+[a-z]{2,63}$")


class UserProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    display_name: str = Field(default="", max_length=80)
    saved_topics: list[str] = Field(default_factory=list, max_length=50)
    preferred_sources: list[str] = Field(default_factory=list)
    blocked_sources: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(default_factory=list, max_length=100)
    default_depth: Depth = "standard"

    @field_validator("saved_topics")
    @classmethod
    def _topics(cls, value: list[str]) -> list[str]:
        out = [" ".join(t.split())[:120] for t in value]
        return [t for t in dict.fromkeys(out) if t]

    @field_validator("preferred_sources", "blocked_sources")
    @classmethod
    def _sources(cls, value: list[str]) -> list[str]:
        unknown = [t for t in value if t not in SOURCE_TOOLS]
        if unknown:
            raise ValueError(f"unknown source tools: {', '.join(unknown)}")
        return list(dict.fromkeys(value))

    @field_validator("blocked_domains")
    @classmethod
    def _domains(cls, value: list[str]) -> list[str]:
        out = []
        for raw in value:
            d = normalize_domain(raw)
            if d and not _DOMAIN.match(d):
                raise ValueError(f"invalid domain: {raw[:60]}")
            out.append(d)
        return [d for d in dict.fromkeys(out) if d]

    def model_post_init(self, __context: Any) -> None:
        clash = set(self.preferred_sources) & set(self.blocked_sources)
        if clash:
            raise ValueError(f"sources both preferred and blocked: {', '.join(sorted(clash))}")


def load_profile(store: AssistantStore) -> UserProfile:
    data = store.get_json(PROFILE_KEY)
    try:
        return UserProfile.model_validate(data or {})
    except ValueError:
        return UserProfile()


def save_profile(store: AssistantStore, profile: UserProfile | dict[str, Any]) -> UserProfile:
    validated = profile if isinstance(profile, UserProfile) else UserProfile.model_validate(profile)
    store.put_json(PROFILE_KEY, validated.model_dump())
    return validated


def read_profile(db_path: Path) -> UserProfile | None:
    """Profile from an existing store file, without creating one (``None`` when absent)."""
    if not Path(db_path).exists():
        return None
    return load_profile(AssistantStore(db_path))


def apply_profile(settings: Settings, profile: UserProfile | None) -> Settings:
    """Copy of ``settings`` with the profile's preferences applied (planner + live agent read these)."""
    if profile is None:
        return settings
    enabled = [t for t in settings.enabled_source_tools() if t not in profile.blocked_sources]
    iterations, results, fetch = DEPTHS[profile.default_depth]
    update: dict[str, Any] = {
        "live_source_tools": ",".join(enabled) if enabled else "none",
        "preferred_source_tools": ",".join(t for t in profile.preferred_sources if t in enabled),
        "blocked_domains": ",".join(profile.blocked_domains),
        "max_research_iterations": iterations,
        "live_max_search_results": results,
        "live_max_fetch_pages": fetch,
    }
    return settings.model_copy(update=update)
