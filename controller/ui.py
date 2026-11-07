"""Read-only supervisor web page (C11): ``python -m controller ui --state-dir state [--db path] [--port 8800]``.

A separate process, never started by run.sh. Server-rendered HTML (no JS), one read-only SQLite connection per request
(``mode=ro``), so it can run beside a live controller (WAL). Pages: ``/`` workflows with health badges,
``/wf/{id}`` the one-screen view, ``/weekly``, ``/why?workflow=&at=``, ``/status``.
"""
from __future__ import annotations

import logging
import socket
import sqlite3
import sys
from contextlib import contextmanager
from html import escape as e
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from . import audit, audit_render, metrics
from . import view as text_view
from . import view_data as vd
from .audit import fmt_minute

log = logging.getLogger(__name__)

CSS = """:root{--bg:#fafaf7;--fg:#1d2327;--mut:#5f6b73;--card:#fff;--line:#dcdfe3;--ok:#1b7f3b;--warn:#a86400;--bad:#b3261e}
@media(prefers-color-scheme:dark){:root{--bg:#15191c;--fg:#e6e8ea;--mut:#9aa5ad;--card:#1d2226;--line:#333b41;--ok:#4cc38a;--warn:#e0a030;--bad:#ff7b72}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:12px 16px 40px}nav a{margin-right:14px;color:var(--mut)}h1{font-size:1.3rem}
h2{font-size:1rem;margin:22px 0 6px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut)}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:8px 0;overflow-x:auto}
.badge{display:inline-block;padding:1px 9px;border-radius:99px;border:1px solid currentColor;font-size:.8rem}
.ok{color:var(--ok)}.attention{color:var(--warn)}.page{color:var(--bad)}table{border-collapse:collapse;width:100%}
td,th{padding:3px 8px 3px 0;text-align:left;vertical-align:top;font-size:.9rem}th{color:var(--mut);font-weight:500}
pre{white-space:pre-wrap;word-break:break-word;margin:0;font-size:.85rem}code{font-size:.85rem}a{color:inherit}
.mut{color:var(--mut)}"""


@contextmanager
def _store(db: Path) -> Iterator[vd.ReadOnlyStore]:
    s = vd.open_readonly(db)
    try:
        yield s
    finally:
        s.close()


def _page(title: str, body: str) -> HTMLResponse:
    nav = '<nav><a href="/">Workflows</a><a href="/weekly">Weekly</a><a href="/status">Status</a></nav>'
    return HTMLResponse(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
                        f'content="width=device-width,initial-scale=1"><title>{e(title)}</title><style>{CSS}</style></head>'
                        f"<body><main>{nav}<h1>{e(title)}</h1>{body}</main></body></html>")


def _card(inner: str) -> str:
    return f'<div class="card">{inner}</div>'


def _none(text: str) -> str:
    return f'<p class="mut">{e(text)}</p>'


def _table(head: list[str], body: list[list[str]]) -> str:
    th = "".join(f"<th>{e(h)}</th>" for h in head)
    tr = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in body)
    return f"<table><tr>{th}</tr>{tr}</table>"


def _tested(d: dict) -> str:
    out = ""
    exps = [x for x in d["experiments"] if x["phase"] in ("pending", "running")]
    for x in exps:
        rows = []
        for arm in sorted(x["live_shares"], key=lambda a: (-x["live_shares"][a], a)):
            a = x["arms"].get(arm)
            est = lift = "-"
            n = ""
            if a and a["engagement"] is not None:
                lo, hi = a["ci"]
                est = f"{text_view.pct(a['engagement'])} <span class='mut'>[{text_view.pct(lo)}, {text_view.pct(hi)}]</span>"
                n = str(a["connects"])
                if arm != x["control"]:
                    li = a["lift_ci"]
                    lift = text_view.signed(a["lift"]) + (f" <span class='mut'>[{text_view.signed(li[0])}, {text_view.signed(li[1])}]</span>" if li else "")
            state = x["arm_states"].get(arm) or ("control" if arm == x["control"] else "-")
            rows.append([e(arm), f"{x['live_shares'][arm] * 100:.1f}%", e(state), n, est, lift])
        bound = x["last_look"].get("boundaries") or {}
        look = f"look {x['looks']}/{x['max_looks']}" + (f", boundary z&gt;{max(bound.values()):.2f}" if bound else "")
        out += _card(f"<b>{e(x['dim'])}</b> <span class='mut'>{e(x['phase'])}, control {e(x['control'])}, {look}</span>"
                     + _table(["arm", "share", "state", "connects", "engagement (95% CI)", "lift vs control"], rows))
    out = out or _none("No experiment running.")
    if d["did_probes"]:
        out += _card("<b>Caller-ID probes</b> " + ", ".join(f"{e(p['did'])} {p['share'] * 100:.1f}% <span class='mut'>[{p['status']}]</span>"
                                                          for p in d["did_probes"]))
    return out


def _winning(d: dict) -> str:
    out = ""
    for w in d["winners"]:
        decay = "no decay check yet"
        if w["checked"]:
            decay = (f"CUSUM S={w['cusum_S']} over {w['cusum_blocks']} blocks{' <b>ALARM</b>' if w['cusum_alarm'] else ''}; "
                     f"holdout recent lift {text_view.signed(w['holdout_recent_lift'])} (n={w['holdout_connects']})"
                     + ("; <b>rollback proposed</b>" if w["rollback"] else ""))
        out += _card(f"<b>{e(w['dim'])}</b>: {e(w['winner'])} promoted over {e(w['old_control'])} "
                     f"<span class='mut'>({e(str(w['scope']))}, since {fmt_minute(w['since'])})</span><br>"
                     f"<span class='mut'>10% holdout on {e(w['old_control'])}. Decay monitor: {decay}</span>")
    return out or _none("Nothing promoted yet.")


def _needs(d: dict) -> str:
    out = ""
    for a in d["approvals"]:
        when = (f"apply by {fmt_minute(a['apply_by_minute'])}" if a["state"] == "APPROVED"
                else f"expires {fmt_minute(a['expires_minute'])}")
        out += _card(f"<b>{e(a['summary'] or a['kind'])}</b><br><span class='mut'>{e(a['state'])} - impact {a['impact_score']}, "
                     f"affected leads {a['affected_leads']}, {when}</span>")
    return out or _none("Nothing waiting on you.")


def _changed(d: dict) -> str:
    out = ""
    for c in d["changes"]:
        why = f"/why?workflow={quote(d['workflow'])}&amp;at={quote(c['at'])}"
        out += _card(f"<b>v{c['version']}</b> {e(c['at'])} by {e(str(c['actor']))}: {e(', '.join(c['changed_dims']) or '-')}"
                     f"<br>{e(c['reason'])}<br><a href=\"{why}\">why</a>")
    return out or _none("No config changes today.")


def _alerts(d: dict) -> str:
    out = "".join(_card(f"<span class='badge page'>incident</span> #{i['id']} {e(i['kind'])} since {e(i['opened'])} {e(str(i['detail']))}")
                  for i in d["incidents"])
    for sev in ("page", "digest"):
        out += "".join(_card(f"<span class='badge {'page' if sev == 'page' else 'attention'}'>{sev}</span> "
                             f"{e(fmt_minute(a['sim_minute']))} <b>{e(a['code'])}</b><br>{e(a['message'])}") for a in d["alerts"][sev])
    if d["alerts"]["other"]:
        out += _none("Other (24h): " + ", ".join(f"{o['code']} x{o['count']}" for o in d["alerts"]["other"]))
    return out or _none("None.")


def _kpis(d: dict) -> str:
    k = d["kpis"]
    return _card(_table(["KPI", f"day {k['day']} so far", "trailing 3 days", "volume"],
                        [["Pickup", text_view.pct(k["pickup"]["today"]), text_view.pct(k["pickup"]["trailing"]),
                          f"{k['pickup']['dials_today']} / {k['pickup']['dials_trailing']} dials"],
                         ["Engagement", text_view.pct(k["engagement"]["today"]), text_view.pct(k["engagement"]["trailing"]),
                          f"{k['engagement']['connects_today']} / {k['engagement']['connects_trailing']} connects"]]))


def workflow_html(d: dict) -> str:
    person = ""
    if d.get("person"):
        person = "<h2>Per-person call time (analysis only)</h2>" + _card(
            "<pre>" + e("\n".join(text_view.person_lines(d["person"]))) + "</pre>")
    return (f"<p class='mut'>as of {e(d['now_text'])}</p><h2>What's being tested</h2>{_tested(d)}<h2>What's winning</h2>{_winning(d)}"
            f"<h2>What we need from you</h2>{_needs(d)}<h2>What changed today</h2>{_changed(d)}"
            f"<h2>Open incidents &amp; alerts</h2>{_alerts(d)}<h2>KPIs</h2>{_kpis(d)}{person}")


def create_app(db: str | Path) -> FastAPI:
    db = Path(db)
    app = FastAPI(title="Controller supervisor view", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(sqlite3.Error)
    def unavailable(request: Request, exc: sqlite3.Error) -> HTMLResponse:
        log.warning("database unavailable for %s: %s", request.url.path, exc)
        return HTMLResponse(_page("Data unavailable", _card("<p>Data unavailable (the database is busy or unreadable). "
                                                           "Please retry in a few seconds.</p>")).body.decode(), status_code=503)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        with _store(db) as s:
            now = vd.now_minute(s)
            rows = [[f"<a href='/wf/{quote(w)}'>{e(w)}</a>", f"<span class='badge {h}'>{h}</span>"]
                    for w in vd.workflow_ids(s) for h in [vd.health(s, w, now)]]
        return _page("Workflows", f"<p class='mut'>as of {e(fmt_minute(now))}</p>" + _card(_table(["workflow", "health"], rows)))

    @app.get("/wf/{wid}", response_class=HTMLResponse)
    def workflow(wid: str, at: str | None = None) -> HTMLResponse:
        with _store(db) as s:
            if wid not in vd.workflow_ids(s):
                raise HTTPException(404, f"unknown workflow {wid}")
            try:
                now = audit.parse_at(at) if at else None
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
            return _page(wid, workflow_html(vd.build(s, wid, now)))

    @app.get("/weekly", response_class=HTMLResponse)
    def weekly() -> HTMLResponse:
        with _store(db) as s:
            now = vd.now_minute(s)
            items = vd.weekly_auto_applies(s, None, now)
        return _page("Weekly: operational auto-applies", _card(f"<pre>{e(text_view.render_weekly(items, now, None))}</pre>"))

    @app.get("/why", response_class=HTMLResponse)
    def why(workflow: str, at: str) -> HTMLResponse:
        try:
            minute = audit.parse_at(at)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        with _store(db) as s:
            return _page(f"Why {workflow} at {at}", _card(f"<pre>{e(audit_render.render_at(audit.why_at(s, workflow, minute)))}</pre>"))

    @app.get("/status", response_class=HTMLResponse)
    def status() -> HTMLResponse:
        with _store(db) as s:
            return _page("Status", _card(f"<pre>{e(metrics.render(metrics.collect(s)))}</pre>"))

    return app


def serve(db: Path, port: int, host: str = "127.0.0.1") -> int:
    """Run the app; a busy port exits 1 with a clear message instead of a traceback."""
    import uvicorn
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError as exc:
            print(f"cannot listen on {host}:{port} ({exc.strerror or exc}); pick another with --port", file=sys.stderr)
            return 1
    print(f"serving {db} read-only on http://{host}:{port}/ (ctrl-c to stop)", file=sys.stderr)
    uvicorn.run(create_app(db), host=host, port=port, log_level="warning")
    return 0
