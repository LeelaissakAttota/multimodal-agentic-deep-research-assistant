"""``deep-research-worker``: run queued research jobs from Redis (RQ).

On Windows (no ``os.fork``) the RQ ``SimpleWorker`` is used automatically: jobs run in the worker
process itself, one at a time. ``--simple`` forces it elsewhere. Without ``MDRA_REDIS_URL`` there is
nothing to work on: runs already execute in-process inside the control API.
"""

from __future__ import annotations

import argparse
import sys

from deep_research.scale.config import ScaleSettings
from deep_research.scale.queue import needs_simple_worker, redis_connection


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MDRA research worker (RQ)")
    parser.add_argument("--simple", action="store_true", help="use SimpleWorker (no fork; default on Windows)")
    parser.add_argument("--burst", action="store_true", help="exit when the queue is empty")
    args = parser.parse_args(argv)
    scale = ScaleSettings()
    if not scale.secret("redis_url"):
        print("MDRA_REDIS_URL is not set: runs execute in-process in the control API; no worker needed.")
        return 2
    try:
        from rq import Queue, SimpleWorker, Worker
    except ImportError:
        print("Install the queue extra: pip install -e .[queue]")
        return 2
    connection = redis_connection(scale)
    simple = args.simple or needs_simple_worker()
    cls = SimpleWorker if simple else Worker
    worker = cls([Queue(scale.rq_queue, connection=connection)], connection=connection)
    print(f"worker listening on queue '{scale.rq_queue}' ({'SimpleWorker' if simple else 'forking Worker'})")
    worker.work(burst=args.burst, with_scheduler=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
