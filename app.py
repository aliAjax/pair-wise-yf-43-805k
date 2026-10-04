import argparse
import signal
import sys
from datetime import date
from pathlib import Path

from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class FixedClock:
    """Deterministic business clock used for demos and tests."""

    def __init__(self, start):
        self.current = date.fromisoformat(start)

    def now(self):
        return self.current


def main(argv=None):
    parser = argparse.ArgumentParser(description="实验室仪器校准与方法验证")
    parser.add_argument("--db", default="./data.db", help="SQLite database path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8309)
    parser.add_argument(
        "--date",
        default=None,
        help="固定业务日期 YYYY-MM-DD（默认使用系统当天，用于演示时效链）",
    )
    args = parser.parse_args(argv)

    repository = SQLiteRepository(args.db)
    clock = FixedClock(args.date) if args.date else None
    rules = RuleEngine(clock=clock)
    service = DomainService(repository, rules)
    static_dir = Path(__file__).resolve().parent / "static"
    server = create_server(args.host, args.port, service, rules, str(static_dir))

    # Replay durable late commits and re-audit the chain on every startup so
    # status and review to-do lists survive restarts.
    summary = service.reconcile()

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        print("实验室仪器校准与方法验证 listening on http://%s:%s" % (args.host, args.port), flush=True)
        print(
            "startup reconcile: pending applied=%s failed=%s held=%s, "
            "chain flagged=%s, open reviews=%s, pending=%s, as_of=%s"
            % (
                summary["pending_drain"]["applied"],
                summary["pending_drain"]["failed"],
                summary["pending_drain"]["held"],
                summary["chain_sweep"]["flagged"],
                summary["open_reviews"],
                summary["pending_changes"],
                summary["as_of"],
            ),
            flush=True,
        )
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
