"""Assistant features on top of finished reports: follow-up chat, profile, notes, reminders, sharing.

Everything is stored locally in one SQLite file under the git-ignored data directory
(``<memory_dir>/assistant.db``, override with ``DEEP_RESEARCH_ASSISTANT_DB``).
"""

from deep_research.assistant.store import AssistantStore, default_db_path

__all__ = ["AssistantStore", "default_db_path"]
