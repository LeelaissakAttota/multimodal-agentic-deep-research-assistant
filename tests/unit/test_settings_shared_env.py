"""The main Settings and ScaleSettings share one .env; MDRA_* keys must not crash Settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from deep_research.core.config import Settings
from deep_research.scale.config import ScaleSettings

PW = "envfile-pw-123"


def test_dotenv_with_mdra_keys_loads_both_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "DEEP_RESEARCH_RESEARCH_MODE=offline\n"
        f"MDRA_REDIS_URL=redis://:{PW}@localhost:6379/0\n"
        f"MDRA_DATABASE_URL=postgresql://mdra:{PW}@localhost:5433/mdra\n"
        "MDRA_S3_BUCKET=mdra-artifacts\n"
        "SOME_OTHER_TOOL_KEY=x\n",
        encoding="utf-8",
    )
    for key in ("MDRA_REDIS_URL", "MDRA_DATABASE_URL", "MDRA_S3_BUCKET", "DEEP_RESEARCH_RESEARCH_MODE"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(_env_file=env)
    assert settings.research_mode == "offline"
    assert not hasattr(settings, "mdra_redis_url")
    scale = ScaleSettings(_env_file=env)
    assert scale.secret("redis_url").endswith("@localhost:6379/0")
    assert scale.secret("database_url").endswith("@localhost:5433/mdra") and scale.s3_bucket == "mdra-artifacts"
    assert PW not in repr(settings) + repr(scale)


def test_default_env_file_with_mdra_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("MDRA_REDIS_URL=redis://localhost:6379/0\nMDRA_PG_PORT=5433\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEP_RESEARCH_ENV_FILE", raising=False)
    Settings(_env_file=".env")  # must not raise "Extra inputs are not permitted"
