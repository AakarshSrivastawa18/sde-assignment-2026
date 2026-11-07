"""CLI.

  python -m controller run    --url http://127.0.0.1:8700 [--db state/controller.db] [--kill-at "D2 13:00"]
  python -m controller view   [--db ...] [--workflow nw_renewal]          # supervisor view (C11)
  python -m controller why    --workflow nw_renewal --at "D3 14:00"       # audit (C12)
  python -m controller health [--db ...]                                  # loop health + alerts (C14)
  python -m controller person --person nw_renewal:p0001234 [--day 3]      # what did this customer hear and why
"""
import argparse
import logging
import os
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(prog="controller")
    sub = ap.add_subparsers(dest="cmd", required=True)
    default_db = os.environ.get("CONTROLLER_DB", "state/controller.db")
    r = sub.add_parser("run")
    r.add_argument("--url", required=True)
    r.add_argument("--db", default=default_db)
    r.add_argument("--kill-at", help="crash test: hard-exit when the sim reaches this minute, e.g. 'D2 13:00'")
    for name in ("view", "why", "health", "person"):
        p = sub.add_parser(name)
        p.add_argument("--db", default=default_db)
        p.add_argument("--workflow")
        p.add_argument("--at")
        p.add_argument("--person")
        p.add_argument("--day", type=int)
        p.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if a.cmd == "run":
        from .loop import Controller
        from .simtime import parse_when
        Controller(a.url, a.db).run(kill_at_minute=parse_when(a.kill_at) if a.kill_at else None)
        return
    from . import views
    from .store import Store
    st = Store(a.db)
    if a.cmd == "view":
        print(views.supervisor_view(st, a.workflow, as_json=a.json))
    elif a.cmd == "why":
        print(views.why(st, a.workflow, a.at, as_json=a.json))
    elif a.cmd == "health":
        print(views.health(st, as_json=a.json))
    elif a.cmd == "person":
        print(views.person(st, a.person, a.day, as_json=a.json))


if __name__ == "__main__":
    main()
