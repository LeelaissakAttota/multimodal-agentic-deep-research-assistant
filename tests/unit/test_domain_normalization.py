"""Blocked-domain normalization shared by config, profile and the live agent."""

from __future__ import annotations

import pytest

from deep_research.assistant.profile import UserProfile
from deep_research.core.agents.live_research_agent import LiveResearchAgent
from deep_research.core.config import Settings
from deep_research.security.domains import normalize_domain


@pytest.mark.parametrize("raw", ["spam.net", "SPAM.NET", "spam.net.", "spam.net:8080", "https://spam.net/x",
                                 "http://www.spam.net:443/a?b#c", "//spam.net", "user@spam.net", " ftp://Spam.Net.. ",
                                 "*.spam.net", "spam.net?x=1"])
def test_variants_normalize_to_bare_host(raw: str) -> None:
    assert normalize_domain(raw) == "spam.net"


def test_empty_and_ipv6() -> None:
    assert normalize_domain("") == "" and normalize_domain("   ") == ""
    assert normalize_domain("http://[::1]:80/x") == "[::1]"


def test_config_list_normalizes_and_dedupes() -> None:
    s = Settings(_env_file=None, blocked_domains="spam.net:8080, https://spam.net/x,,Evil.org.,www.evil.org")
    assert s.blocked_domain_list() == ["spam.net", "evil.org"]


def test_profile_accepts_port_and_url_forms() -> None:
    p = UserProfile(blocked_domains=["spam.net:8080", "https://spam.net/x", "Other.example."])
    assert p.blocked_domains == ["spam.net", "other.example"]
    with pytest.raises(ValueError):
        UserProfile(blocked_domains=["http://not a domain/"])


def test_live_agent_normalizes_configured_entries() -> None:
    agent = LiveResearchAgent(search_tool=None, fetch_tool=None, blocked_domains=["https://spam.net:8080/x"])  # type: ignore[arg-type]
    assert agent.blocked_domains == ["spam.net"]
    assert agent.is_blocked("http://a.spam.net:9000/p") and not agent.is_blocked("https://spam.network/")
