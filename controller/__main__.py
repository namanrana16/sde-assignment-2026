"""CLI: ``python -m controller run|status|why|view|ui``."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

from . import audit, settings
from .client import SimClient, SimUnavailable
from .loop import Controller, WorldRegressed
from .store import Store
from .world_id import open_world_store

log = logging.getLogger("controller")
EXIT_WORLD_REGRESSED = 4


def _wait_for_sim(client: SimClient, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            client.health()
            return
        except Exception as exc:  # noqa: BLE001 - any failure means "not up yet"
            if time.monotonic() > deadline:
                raise SimUnavailable(f"simulator not reachable: {exc}") from exc
            time.sleep(2)


def cmd_run(args: argparse.Namespace) -> int:
    try:
        return _run(args)
    except WorldRegressed as exc:
        log.error("%s", exc)
        return EXIT_WORLD_REGRESSED


def _run(args: argparse.Namespace) -> int:
    client = SimClient(base_url=args.url)
    _wait_for_sim(client, args.startup_wait if args.startup_wait is not None else settings.STARTUP_WAIT_S)
    store, world, fresh = open_world_store(client, args.state_dir)
    log.info("world %s fp=%s (%s db)", world.scenario, world.fingerprint[:8],
             "new" if fresh else "existing")
    stop = args.until_minute
    if args.max_sim_minutes is not None:
        stop = client.state()["sim_minute"] + args.max_sim_minutes
    profile_out = os.environ.get("CONTROLLER_PROFILE")  # dev knob: cProfile the whole run to this file
    if profile_out:
        import cProfile
        prof = cProfile.Profile()
        try:
            result = prof.runcall(Controller(client, store, stop_at_minute=stop).run)
        finally:
            prof.dump_stats(profile_out)
    else:
        result = Controller(client, store, stop_at_minute=stop).run()
    log.info("done: finished=%s ticks=%d sim_minute=%d events=%d", result.finished,
             result.ticks, result.sim_minute, result.stats.events)
    if not result.finished and stop is None:
        return 1
    return 0


def _latest_db(state_dir: str) -> Path | None:
    def activity(p: Path) -> float:  # under WAL a live DB's main file is rarely touched: its -wal file is
        wal = p.with_name(p.name + "-wal")
        return max(p.stat().st_mtime, wal.stat().st_mtime if wal.exists() else 0.0)

    dbs = sorted(Path(state_dir).glob("*.db"), key=activity, reverse=True)
    return dbs[0] if dbs else None


def _resolve_db(args: argparse.Namespace) -> Path | None:
    if getattr(args, "db", None):
        return Path(args.db)
    return _latest_db(args.state_dir)


def _build_why(store: Store, args: argparse.Namespace) -> tuple[dict, str] | None:
    from . import audit_render as ar
    if args.lead:
        rep = audit.why_lead(store, args.lead, args.day)
        return rep, ar.render_lead(rep, args.verbose)
    if not args.workflow:
        raise ValueError("--workflow is required unless --lead is given")
    if args.at:
        rep = audit.why_at(store, args.workflow, audit.parse_at(args.at))
        return rep, ar.render_at(rep, args.verbose)
    if args.from_ and args.to:
        rep = audit.why_range(store, args.workflow, audit.parse_bound(args.from_),
                              audit.parse_bound(args.to, end=True))
        return rep, ar.render_range(rep, args.verbose)
    raise ValueError("give --at, or --from and --to, or --lead")


def cmd_why(args: argparse.Namespace) -> int:
    db = _resolve_db(args)
    if db is None or not db.exists():
        print(f"no state database found ({db or args.state_dir})")
        return 1
    store = _open_ro(args)  # audit only reads: never run the schema script / migrations on a live DB
    if store is None:
        return 1
    try:
        built = _build_why(store, args)
    except ValueError as exc:
        print(exc)
        return 2
    finally:
        store.close()
    rep, text = built  # type: ignore[misc]
    print(json.dumps(rep, indent=2, default=str) if args.json else text)
    return 0


def _open_ro(args: argparse.Namespace):
    from . import view_data
    db = _resolve_db(args)
    if db is None or not db.exists():
        print(f"no state database found ({db or args.state_dir})")
        return None
    try:
        store = view_data.open_readonly(db)
        # a 0-byte or non-SQLite file only fails on the first read; every controller store has a ``meta`` table
        if not store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone():
            store.close()
            raise sqlite3.DatabaseError("not a controller store (no meta table)")
        return store
    except Exception as exc:  # noqa: BLE001 - a foreign/corrupt file must give a message, not a traceback
        print(f"cannot open {db} read-only: {exc}")
        return None


def cmd_status(args: argparse.Namespace) -> int:
    from . import metrics
    store = _open_ro(args)
    if store is None:
        return 1
    try:
        m = metrics.collect(store)
        if args.json:
            cur = store.get_cursor()
            print(json.dumps({**m, "cursor": {"chunk": cur.last_chunk, "event_id": cur.last_event_id}}, indent=2, default=str))
        else:
            print(metrics.render(m))
    finally:
        store.close()
    return 0


def cmd_view(args: argparse.Namespace) -> int:
    from . import view, view_data
    store = _open_ro(args)
    if store is None:
        return 1
    try:
        now = audit.parse_at(args.at) if args.at else view_data.now_minute(store)
        if args.weekly:
            print(view.render_weekly(view_data.weekly_auto_applies(store, args.workflow, now), now, args.workflow))
            return 0
        known = view_data.workflow_ids(store)
        if not args.workflow:
            print("workflows: " + ", ".join(known) + "\n(give --workflow, or --weekly)")
            return 2
        if args.workflow not in known:
            print(f"unknown workflow {args.workflow!r}; known: {', '.join(known)}")
            return 2
        print(view.render(view_data.build(store, args.workflow, now)))
    except ValueError as exc:
        print(exc)
        return 2
    finally:
        store.close()
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    from . import ui
    db = _resolve_db(args)
    if db is None or not db.exists():
        print(f"no state database found ({db or args.state_dir})")
        return 1
    return ui.serve(db, args.port, args.host)


def cmd_person_analysis(args: argparse.Namespace) -> int:
    from . import person_analysis, view_data
    db = _resolve_db(args)
    if db is None or not db.exists():
        print(f"no state database found ({db or args.state_dir})")
        return 1
    persist = args.persist and not args.no_persist
    store = Store(db) if persist else view_data.open_readonly(db)  # read-only unless --persist is explicit
    try:
        report = person_analysis.run(store, view_data.now_minute(store), persist=persist)
    finally:
        store.close()
    print(json.dumps(report, indent=2) if args.json else person_analysis.render(report))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="controller")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--url", required=True)
    run.add_argument("--state-dir", default=settings.DEFAULT_STATE_DIR)
    run.add_argument("--startup-wait", type=float, default=float(os.environ["CONTROLLER_STARTUP_WAIT_S"])
                     if os.environ.get("CONTROLLER_STARTUP_WAIT_S") else None,
                     help="seconds to wait for the sim at startup (env CONTROLLER_STARTUP_WAIT_S)")
    run.add_argument("--until-minute", type=int, default=None,
                     help="dev: stop (exit 0) once sim_minute reaches this absolute minute")
    run.add_argument("--max-sim-minutes", type=int, default=None,
                     help="dev: stop (exit 0) after this many simulated minutes from now")
    status = sub.add_parser("status")
    status.add_argument("--state-dir", default=settings.DEFAULT_STATE_DIR)
    status.add_argument("--db", default=None, help="explicit database file (default: newest in --state-dir)")
    status.add_argument("--json", action="store_true")
    view = sub.add_parser("view", help="one-screen supervisor summary (read-only)")
    view.add_argument("--state-dir", default=settings.DEFAULT_STATE_DIR)
    view.add_argument("--db", default=None, help="explicit database file (default: newest in --state-dir)")
    view.add_argument("--workflow")
    view.add_argument("--at", help='view as of a time, e.g. "Day 3 14:00" (default: latest)')
    view.add_argument("--weekly", action="store_true", help="operational auto-applies of the last 7 days (Compliance)")
    ui = sub.add_parser("ui", help="read-only web view; separate process, never started by run.sh")
    ui.add_argument("--state-dir", default=settings.DEFAULT_STATE_DIR)
    ui.add_argument("--db", default=None, help="explicit database file (default: newest in --state-dir)")
    ui.add_argument("--port", type=int, default=8800)
    ui.add_argument("--host", default="127.0.0.1")
    why = sub.add_parser("why", help="explain which config was active at a time and why")
    why.add_argument("--state-dir", default=settings.DEFAULT_STATE_DIR)
    why.add_argument("--db", default=None, help="explicit database file (default: newest in --state-dir)")
    why.add_argument("--workflow")
    why.add_argument("--at", help='e.g. "Day 3 14:00"')
    why.add_argument("--from", dest="from_", help='timeline start, e.g. "Day 2"')
    why.add_argument("--to", help='timeline end (a bare day includes that whole day), e.g. "Day 4"')
    why.add_argument("--lead", help="lead id: that customer's calls and the decisions behind them")
    why.add_argument("--day", type=int, default=None, help="with --lead: only calls on this day")
    why.add_argument("--json", action="store_true", help="the complete report as JSON")
    why.add_argument("--verbose", action="store_true",
                     help="also print raw decision inputs, dry runs and every experiment look row")
    pa = sub.add_parser("person-analysis", help="per-person call-time analysis of a store (analysis only, never config)")
    pa.add_argument("--state-dir", default=settings.DEFAULT_STATE_DIR)
    pa.add_argument("--db", default=None, help="explicit database file (default: newest in --state-dir)")
    pa.add_argument("--persist", action="store_true", help="write person_analysis rows and a decision into the store (default: read-only)")
    pa.add_argument("--no-persist", action="store_true", help="deprecated no-op: read-only is already the default")
    pa.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return {"run": cmd_run, "status": cmd_status, "why": cmd_why, "view": cmd_view,
            "ui": cmd_ui, "person-analysis": cmd_person_analysis}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
