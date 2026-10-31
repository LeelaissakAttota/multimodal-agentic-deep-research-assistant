"""``deep-research-migrate``: copy, export or import research sessions between SQLite and Postgres.

    deep-research-migrate copy   --from data/research_memory.sqlite --to postgresql://mdra:mdra@localhost:5433/mdra
    deep-research-migrate export --from data/research_memory.sqlite --out sessions.jsonl
    deep-research-migrate import --to  postgresql://... --in sessions.jsonl

A target of ``env`` means ``MDRA_DATABASE_URL``. Database URLs are never printed with credentials.
Copy/import upsert by session id and keep the immutable-history rule; existing rows are not deleted.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from deep_research.persistence.contracts import ResearchSessionRepository, ResearchSessionSnapshot
from deep_research.persistence.factory import open_repository

MAX_SESSIONS = 1_000_000


def _resolve(target: str) -> str:
    if target == "env":
        from deep_research.scale.config import ScaleSettings

        url = ScaleSettings().secret("database_url")
        if not url:
            raise SystemExit("MDRA_DATABASE_URL is not set")
        return url
    return target


def _label(target: str) -> str:
    if "://" not in target:
        return str(Path(target))
    try:
        from deep_research.persistence.sqlalchemy_repository import safe_url

        return safe_url(target)
    except ImportError:
        return target.split("://", 1)[0] + "://..."


def copy_sessions(src: ResearchSessionRepository, dst: ResearchSessionRepository) -> int:
    dst.initialize()
    count = 0
    for snapshot in src.list_sessions(limit=MAX_SESSIONS):
        dst.save_session(snapshot)
        count += 1
    return count


def export_sessions(src: ResearchSessionRepository, out: Path) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = src.list_sessions(limit=MAX_SESSIONS)
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        for snapshot in rows:
            fh.write(snapshot.model_dump_json() + "\n")
    return len(rows)


def import_sessions(dst: ResearchSessionRepository, src_file: Path) -> int:
    dst.initialize()
    count = 0
    with src_file.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                dst.save_session(ResearchSessionSnapshot.model_validate_json(line))
                count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Copy/export/import MDRA research sessions (SQLite <-> Postgres)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("copy")
    c.add_argument("--from", dest="src", required=True)
    c.add_argument("--to", dest="dst", required=True)
    e = sub.add_parser("export")
    e.add_argument("--from", dest="src", required=True)
    e.add_argument("--out", required=True)
    i = sub.add_parser("import")
    i.add_argument("--to", dest="dst", required=True)
    i.add_argument("--in", dest="infile", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "copy":
        src, dst = _resolve(args.src), _resolve(args.dst)
        n = copy_sessions(open_repository(src), open_repository(dst))
        print(f"copied {n} sessions from {_label(src)} to {_label(dst)}")
    elif args.cmd == "export":
        src = _resolve(args.src)
        n = export_sessions(open_repository(src), Path(args.out))
        print(f"exported {n} sessions from {_label(src)} to {Path(args.out)}")
    else:
        dst = _resolve(args.dst)
        n = import_sessions(open_repository(dst), Path(args.infile))
        print(f"imported {n} sessions into {_label(dst)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
