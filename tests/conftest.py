"""Keep the test suite hermetic: never read the developer's .env or DEEP_RESEARCH_* shell variables.

A local .env with DEEP_RESEARCH_RESEARCH_MODE=live and real keys would otherwise make
offline tests perform live network calls (and hang or cost quota).
"""

from __future__ import annotations

import os

for _name in [n for n in os.environ if n.upper().startswith("DEEP_RESEARCH_")]:
    del os.environ[_name]
os.environ["DEEP_RESEARCH_ENV_FILE"] = os.path.join(os.path.dirname(__file__), "__no_env_file__")
# Long-term memory writes SQLite/vector files; tests opt in explicitly with a tmp dir.
os.environ["DEEP_RESEARCH_LONG_TERM_MEMORY"] = "off"
