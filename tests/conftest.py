"""Keep the test suite hermetic: never read the developer's .env or DEEP_RESEARCH_* shell variables.

A local .env with DEEP_RESEARCH_RESEARCH_MODE=live and real keys would otherwise make
offline tests perform live network calls (and hang or cost quota).
"""

from __future__ import annotations

import os

# MDRA_* scale backends (Redis, Postgres, S3, OTel, Neo4j) are off in tests; integration tests opt in with
# MDRA_TEST_* variables and pass them explicitly.
for _name in [n for n in os.environ if n.upper().startswith(("DEEP_RESEARCH_", "MDRA_"))
              and not n.upper().startswith("MDRA_TEST_") and n.upper() != "MDRA_LIVE_TESTS"]:
    del os.environ[_name]
os.environ["DEEP_RESEARCH_ENV_FILE"] = os.path.join(os.path.dirname(__file__), "__no_env_file__")
# Long-term memory writes SQLite/vector files; tests opt in explicitly with a tmp dir.
os.environ["DEEP_RESEARCH_LONG_TERM_MEMORY"] = "off"
# Self-improvement writes data/learning.db; tests opt in explicitly with a tmp dir.
os.environ["DEEP_RESEARCH_SELF_IMPROVEMENT"] = "false"
# Assistant store (profile/notes/shares): never read or write the developer's data/assistant.db.
os.environ["DEEP_RESEARCH_ASSISTANT_DB"] = os.path.join(
    __import__("tempfile").mkdtemp(prefix="mdra-test-assistant-"), "assistant.db")
