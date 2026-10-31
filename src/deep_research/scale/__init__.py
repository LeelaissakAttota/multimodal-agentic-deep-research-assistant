"""Optional scale-out backends (queue, Postgres, object storage, tracing, graph).

Every backend is **off by default**: with no ``MDRA_*`` variables set, MDRA uses SQLite, local
files and in-process runs exactly as before. Heavy dependencies are optional extras.
"""

from deep_research.scale.config import ScaleSettings, backend_status

__all__ = ["ScaleSettings", "backend_status"]
