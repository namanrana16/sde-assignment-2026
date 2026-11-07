"""C4b per-person call-time analysis: posterior math, qualification rule, persistence, and "never a config change"."""
import json
import math
import re

import pytest

from controller import person_analysis as pa
from controller import settings, view, view_data
from controller.__main__ import main as cli_main
from controller.store import Store

WF = "wf_a"
HOURS = list(range(10, 20))  # window 10-20


# ---------------------------------------------------------------- pure math
def test_beta_sf_matches_closed_forms():
    assert pa.beta_sf(0.3, 1, 1) == pytest.approx(0.7)
    assert pa.beta_sf(0.3, 2, 1) == pytest.approx(1 - 0.09)  # cdf = x^2
    assert pa.beta_sf(0.5, 7, 7) == pytest.approx(0.5)
    assert pa.beta_sf(0.0, 3, 4) == 1.0 and pa.beta_sf(1.0, 3, 4) == 0.0
    x = 0.6  # Beta(5,3): cdf(x) = sum_{j=5}^{7} C(7,j) x^j (1-x)^(7-j)
    cdf = sum(math.comb(7, j) * x ** j * (1 - x) ** (7 - j) for j in range(5, 8))
    assert pa.beta_sf(x, 5, 3) == pytest.approx(1 - cdf, abs=1e-9)


def test_block_edges_cover_window_in_three_or_four_blocks():
    assert len(pa.block_edges(10, 20)) == 4 and len(pa.block_edges(9, 16)) == 3
    for lo, hi in ((10, 20), (9, 21), (9, 16), (10, 18)):
        edges = pa.block_edges(lo, hi)
        assert edges[0][0] == lo and edges[-1][1] == hi
        assert all(a[1] == b[0] for a, b in zip(edges, edges[1:]))
        assert all(e[1] > e[0] for e in edges)


def test_posterior_without_data_equals_prior_and_data_weight_is_n_over_n_plus_k():
    k = settings.PERSON_PRIOR_STRENGTH
    post = pa.posterior(0.4, 0, 0)
    assert post.mean == pytest.approx(0.4) and post.data_weight == 0.0
    post = pa.posterior(0.4, 4, 4)  # 4 dials, 4 connects
    assert post.mean == pytest.approx((0.4 * k + 4) / (k + 4))
    assert post.data_weight == pytest.approx(4 / (k + 4))


# ---------------------------------------------------------------- qualification
FLAT = pa.CohortProfile(rates={"flat": [0.3, 0.3, 0.3, 0.3]}, average={"flat": 0.3})


def person(blocks, cohort="flat", campaigns=2):
    return {"person_id": "p", "cohort": cohort, "campaigns": campaigns, "blocks": blocks}


def test_eligibility_needs_campaigns_dials_and_two_blocks():
    ok = pa.assess(person([(2, 2), (2, 0), (0, 0), (0, 0)]), FLAT)
    assert ok is not None and ok.dials == 4
    assert pa.assess(person([(2, 2), (2, 0), (0, 0), (0, 0)], campaigns=1), FLAT) is None
    assert pa.assess(person([(2, 2), (1, 0), (0, 0), (0, 0)]), FLAT) is None  # 3 dials
    assert pa.assess(person([(4, 4), (0, 0), (0, 0), (0, 0)]), FLAT) is None  # one block only


def test_few_dials_do_not_qualify_but_a_lot_of_consistent_data_does():
    weak = pa.assess(person([(2, 2), (2, 0), (0, 0), (0, 0)]), FLAT)
    assert weak.p_best_above_avg < settings.PERSON_PASS_PROB and not weak.qualifies
    strong = pa.assess(person([(120, 100), (40, 12), (0, 0), (0, 0)]), FLAT)
    assert strong.best_block == 0 and strong.qualifies
    assert strong.p_best_above_avg >= settings.PERSON_PASS_PROB
    assert strong.post_mean_best > strong.prior_mean_best == pytest.approx(0.3)
    assert strong.data_driven  # a flat cohort profile cannot pass on its own


def test_prior_only_pass_is_flagged_so_it_is_not_credited_to_the_person():
    prof = pa.CohortProfile(rates={"peaky": [0.2, 0.2, 0.7, 0.2]}, average={"peaky": 0.3})
    a = pa.assess(person([(2, 0), (2, 0), (0, 0), (0, 0)], cohort="peaky"), prof)
    assert a.prior_only_p >= settings.PERSON_PASS_PROB  # the cohort profile alone would pass
    assert a.qualifies == (a.p_best_above_avg >= settings.PERSON_PASS_PROB)
    assert not a.data_driven


def test_summary_reports_lift_decomposition_and_honest_note():
    rows = [pa.assess(person([(120, 100), (40, 12), (0, 0), (0, 0)]), FLAT),
            pa.assess(person([(2, 2), (2, 0), (0, 0), (0, 0)]), FLAT)]
    s = pa.summarise(rows, total_dials=1000)
    assert s["eligible"] == 2 and s["qualifying"] == 1
    assert s["expected_lift_ignoring_capacity"] > 0
    assert 0 <= s["mean_data_weight"] < 1
    assert s["workflow_lift_upper_bound"] == pytest.approx(
        s["expected_lift_ignoring_capacity"] * s["qualifying_dial_share"], abs=1e-4)
    assert "0.25" in s["note"] and "capacity" in s["note"] and "holdout" in s["note"]


def test_summary_of_nothing_is_zero_not_a_crash():
    s = pa.summarise([], total_dials=0)
    assert s["eligible"] == 0 and s["qualifying"] == 0 and s["expected_lift_ignoring_capacity"] == 0.0


# ---------------------------------------------------------------- store integration
@pytest.fixture()
def seeded(tmp_path):
    s = Store(tmp_path / "state" / "t.db")
    counter = {"lid": 0}

    def add(person_id, campaign, hour, connected, cohort):
        counter["lid"] += 1
        n = counter["lid"]
        s.conn.execute("INSERT INTO leads(lid,lead_id,person_id,workflow_id,cell,campaign_id,ts_minute) VALUES (?,?,?,?,0,?,0)",
                       (n, f"l{n}", person_id, WF, campaign))
        s.conn.execute("INSERT INTO calls(event_id,workflow_id,ts_minute,day,hour,lid,attempt_no,config_version,cohort_id,welcome,"
                       "voice,caller_id,prompt,connected,engaged) VALUES (?,?,?,1,?,?,1,1,?,'w','v','d','p',?,0)",
                       (n, WF, 1440 + hour * 60, hour, n, cohort, connected))

    for cohort in ("c1", "c2"):  # background: cohort pickup exactly 0.3 in every hour
        for i in range(2000):
            add(f"{WF}:bg{cohort}{i}", "c0", HOURS[i % 10], 1 if (i // 10) % 10 < 3 else 0, cohort)
    for i in range(40):  # one returning person with many morning connects
        add(f"{WF}:star", "c1", 10 + i % 2, 1 if i % 6 else 0, "c2")
    for i in range(20):
        add(f"{WF}:star", "c2", 17 + i % 3, 1 if i % 5 == 0 else 0, "c2")
    for i, h in enumerate((10, 10, 17, 18)):  # a returning person with only 4 dials
        add(f"{WF}:few", f"c{i % 2 + 1}", h, 0, "c1")
    yield s
    s.close()


def snapshot(s):
    return {t: s.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("config_versions", "proposals", "proposal_events", "experiments", "cohort_defs")}


def test_run_persists_rows_and_audit_decision_and_changes_no_config(seeded):
    before = snapshot(seeded)
    out = pa.run(seeded, now_minute=2 * 1440)
    assert snapshot(seeded) == before  # no config, proposal or experiment is touched
    rep = out[WF]
    assert rep["eligible"] == 2 and rep["qualifying"] == 1
    rows = seeded.conn.execute("SELECT person_id, qualifies, would_assign_hour FROM person_analysis WHERE workflow_id=?", (WF,)).fetchall()
    assert {r[0] for r in rows} == {f"{WF}:star", f"{WF}:few"}
    star = next(r for r in rows if r[0].endswith("star"))
    assert star[1] == 1 and 10 <= star[2] < 20
    few = next(r for r in rows if r[0].endswith("few"))
    assert few[1] == 0 and few[2] is None  # nothing would be assigned
    dec = seeded.conn.execute("SELECT kind, workflow_id, inputs FROM decisions WHERE kind='person_analysis'").fetchall()
    assert len(dec) == 1 and dec[0][1] == WF
    inputs = json.loads(dec[0][2])
    assert inputs["analysis_only"] is True and inputs["prior_strength"] == settings.PERSON_PRIOR_STRENGTH
    assert inputs["pass_prob"] == settings.PERSON_PASS_PROB and "config_change" not in inputs


def test_module_has_no_path_to_the_config_pipeline():
    src = open(pa.__file__, encoding="utf-8").read()
    imported = set(re.findall(r"^from \.(\w+)|^from \. import ([\w, ]+)", src, re.M))
    names = {n.strip() for pair in imported for part in pair for n in part.split(",") if n.strip()}
    assert not names & {"desired", "planner", "governance", "client", "proposals", "loop"}


def test_rerun_replaces_rows_instead_of_duplicating(seeded):
    pa.run(seeded, now_minute=2 * 1440)
    pa.run(seeded, now_minute=3 * 1440)
    assert seeded.conn.execute("SELECT COUNT(*) FROM person_analysis").fetchone()[0] == 2


def test_daily_job_is_flag_gated_and_runs_once_per_sim_day(seeded, monkeypatch):
    count = lambda: seeded.conn.execute("SELECT COUNT(*) FROM decisions WHERE kind='person_analysis'").fetchone()[0]  # noqa: E731
    monkeypatch.setattr(settings, "ENABLE_PERSON_ANALYSIS", False)
    assert pa.maybe_run_daily(seeded, 2 * 1440) is None and count() == 0
    monkeypatch.setattr(settings, "ENABLE_PERSON_ANALYSIS", True)
    assert pa.maybe_run_daily(seeded, 2 * 1440 + 30) is not None
    assert pa.maybe_run_daily(seeded, 2 * 1440 + 600) is None  # same sim day
    assert pa.maybe_run_daily(seeded, 3 * 1440) is not None
    assert count() == 2


def test_view_and_cli_show_the_analysis_only_section(seeded, capsys):
    pa.run(seeded, now_minute=2 * 1440)
    ro = view_data.open_readonly(seeded.path)
    try:
        d = view_data.build(ro, WF)
        assert d["person"]["analysis_only"] is True and d["person"]["eligible"] == 2
        text = view.render(d)
    finally:
        ro.close()
    assert "Per-person call time (analysis only)" in text and "never pushed to config" in text
    assert cli_main(["person-analysis", "--db", str(seeded.path)]) == 0
    out = capsys.readouterr().out
    assert WF in out and "eligible" in out


def test_view_without_analysis_has_no_person_section(tmp_path):
    s = Store(tmp_path / "state" / "e.db")
    s.close()
    ro = view_data.open_readonly(s.path)
    try:
        assert view_data.build(ro, WF)["person"] is None
    finally:
        ro.close()


# ---------------------------------------------------------------- null replay (selection effect)
def test_null_replay_counts_chance_qualifiers_deterministically():
    persons = [person([(5, 1), (5, 2), (4, 1), (4, 2)]) for _ in range(300)]
    prof = pa.CohortProfile(rates={"flat": [0.3, 0.3, 0.3, 0.3]}, average={"flat": 0.3})
    a = pa.null_replay(persons, prof, seed=1)
    assert a == pa.null_replay(persons, prof, seed=1)
    assert 0 <= a["null_qualifying"] <= 300 and a["null_expected_lift"] >= 0
    rich = pa.null_replay([person([(60, 18), (60, 18), (60, 18), (60, 18)])] * 300, prof, seed=1)
    assert rich["null_qualifying"] < 300 * 0.7  # best-of-4 selection qualifies many noise persons, never all


def test_summary_carries_null_comparison_and_ui_renders_section(seeded):
    out = pa.run(seeded, now_minute=2 * 1440)[WF]
    assert "null_qualifying" in out and "null_expected_lift" in out
    from controller import ui
    ro = view_data.open_readonly(seeded.path)
    try:
        html = ui.workflow_html(view_data.build(ro, WF))
    finally:
        ro.close()
    assert "Per-person call time (analysis only)" in html
