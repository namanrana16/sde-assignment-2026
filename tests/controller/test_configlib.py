"""Config builder, TV and changed dims, cross-checked against the sim's own functions."""
import copy
import itertools

import pytest

from controller import configlib as cl
from sim import world as simw

WF = {
    "workflow_id": "w", "dialling_window": [9, 13],
    "options": {"welcome_message": ["W1", "W2", "W3"], "voice": ["V1", "V2"],
                "caller_id": ["D1", "D2", "D3"], "prompt_version": ["P1", "P2"]},
}
BASE = {
    "cohorts": [{"id": "hi", "where": {"lang": ["hi"]}}, {"id": "young", "where": {"age_band": ["18-30"]}}],
    "allocations": {"default": {"welcome_message": {"W1": 1.0}, "voice": {"V1": 1.0},
                                "caller_id": {"D1": 0.5, "D2": 0.5}},
                    "hi": {"voice": {"V1": 0.5, "V2": 0.5}}},
    "prompt_version": "P1",
    "schedule": {"hour_weights": {"hi": {"9": 1.0, "10": 2.0, "11": 1.0, "12": 1.0}},
                 "lead_preferred_hour": {}},
}
BASE = cl.sim_normalise(WF, BASE)


def test_cohorts_copied_verbatim_in_order():
    new = cl.apply_patch(WF, BASE, {"allocations": {"default": {"welcome_message": {"W1": 1, "W2": 1}}}})
    assert new["cohorts"] == BASE["cohorts"]
    assert new["allocations"]["default"]["welcome_message"] == {"W1": 0.5, "W2": 0.5}
    assert new["allocations"]["hi"] == BASE["allocations"]["hi"]  # untouched entries verbatim
    assert cl.changed_dims(BASE, new) == ["welcome_message"]


def test_patch_never_mutates_inputs():
    snap = copy.deepcopy(BASE)
    cl.apply_patch(WF, BASE, {"allocations": {"hi": {"voice": None}}, "prompt_version": "P2"})
    assert BASE == snap


def test_normalises_and_drops_zero_weights():
    new = cl.apply_patch(WF, BASE, {"allocations": {"default": {"caller_id": {"D1": 2, "D2": 2, "D3": 0}}}})
    assert new["allocations"]["default"]["caller_id"] == {"D1": 0.5, "D2": 0.5}
    new = cl.apply_patch(WF, BASE, {"allocations": {"default": {"caller_id": {"D1": 1, "D2": 1, "D3": 2}}}})
    assert new["allocations"]["default"]["caller_id"] == {"D1": 0.25, "D2": 0.25, "D3": 0.5}


@pytest.mark.parametrize("patch,msg", [
    ({"allocations": {"default": {"caller_id": {"D9": 1}}}}, "not allowed"),
    ({"allocations": {"default": {"caller_id": {"D1": -1}}}}, ">= 0"),
    ({"allocations": {"default": {"caller_id": {"D1": 0}}}}, "sum to 0"),
    ({"allocations": {"default": {"voice": None}}}, "cannot drop"),
    ({"allocations": {"ghost": {"voice": {"V1": 1}}}}, "not a defined cohort"),
    ({"prompt_version": "P9"}, "not allowed"),
    ({"hour_weights": {"hi": {"8": 1}}}, "outside"),
    ({"hour_weights": {"hi": {"9": 0}}}, "zero weight"),
    ({"cohorts": [{"id": "default", "where": {}}]}, "unique"),
    ({"cohorts": [{"id": "x", "where": {"bogus": ["a"]}}]}, "unknown feature"),
])
def test_invalid_patches_raise(patch, msg):
    with pytest.raises(cl.ProposalError, match=msg):
        cl.apply_patch(WF, BASE, patch)


def test_every_in_window_hour_present_in_each_touched_table():
    new = cl.apply_patch(WF, BASE, {"hour_weights": {"young": {"10": 3, "11": 3}, "default": {"9": 2}}})
    for key in ("young", "default"):
        table = new["schedule"]["hour_weights"][key]
        assert set(table) == {"9", "10", "11", "12"} and all(w > 0 for w in table.values())
    assert new["schedule"]["hour_weights"]["young"]["9"] == 3.0  # neutral fill = mean of given
    assert new["schedule"]["hour_weights"]["hi"] == BASE["schedule"]["hour_weights"]["hi"]


def test_zero_hour_weight_only_when_explicit():
    new = cl.apply_patch(WF, BASE, {"hour_weights": {"hi": {"9": 0, "10": 1}}, "allow_zero_hours": True})
    t = new["schedule"]["hour_weights"]["hi"]
    assert t["9"] == 0 and set(t) == {"9", "10", "11", "12"}


def test_replacing_cohorts_prunes_dependent_keys():
    new = cl.apply_patch(WF, BASE, {"cohorts": [{"id": "young", "where": {"age_band": ["18-30"]}}]})
    assert "hi" not in new["allocations"] and "hi" not in new["schedule"]["hour_weights"]
    assert "cohorts" in cl.changed_dims(BASE, new)


def test_hour_table_can_be_dropped_and_override_removed():
    new = cl.apply_patch(WF, BASE, {"hour_weights": {"hi": None}, "allocations": {"hi": {"voice": None}}})
    assert new["schedule"]["hour_weights"] == {} and cl.changed_dims(BASE, new) == ["voice", "schedule"]


def _variants():
    base = BASE
    out = [base]
    for patch in [
        {"allocations": {"default": {"welcome_message": {"W1": 1, "W2": 1}}}},
        {"allocations": {"default": {"caller_id": {"D3": 1}}}},
        {"allocations": {"hi": {"voice": {"V2": 1}}}},
        {"allocations": {"hi": {"voice": {"V1": 0.7, "V2": 0.3}}, "young": {"caller_id": {"D1": 1}}}},
        {"cohorts": [{"id": "young", "where": {"age_band": ["18-30"]}}]},
        {"cohorts": [{"id": "z", "where": {"lang": ["ta"]}}] + base["cohorts"]},
        {"prompt_version": "P2"},
        {"hour_weights": {"default": {"9": 5, "12": 1}}},
        {"lead_preferred_hour": {"p1": 10}},
    ]:
        try:
            out.append(cl.apply_patch(WF, base, patch))
        except cl.ProposalError:
            pass
    return out


def test_builder_output_is_what_the_sim_stores():
    for cfg in _variants():
        assert simw.normalise_config(WF, cfg) == cfg  # a fixed point of the sim's normaliser


def test_changed_dims_and_tv_identical_to_sim():
    vs = _variants()
    assert len(vs) >= 8
    for old, new in itertools.product(vs, vs):
        assert cl.changed_dims(old, new) == simw.changed_dims(old, new)
        assert cl.max_shift(old, new) == pytest.approx(simw.max_shift(old, new))


def test_tv_values_per_cohort_and_dim():
    new = cl.apply_patch(WF, BASE, {"allocations": {"default": {"caller_id": {"D3": 1}}}})
    tv = cl.tv_by_cohort(BASE, new)
    assert tv[("default", "caller_id")] == pytest.approx(1.0)
    assert tv[("hi", "caller_id")] == pytest.approx(1.0)  # hi inherits default for caller_id
    assert tv[("hi", "voice")] == 0 and cl.max_shift(BASE, BASE) == 0


def test_diff_nonempty_when_changed():
    new = cl.apply_patch(WF, BASE, {"prompt_version": "P2", "hour_weights": {"hi": {"9": 3}}})
    d = cl.config_diff(BASE, new)
    assert d["prompt_version"] == {"old": "P1", "new": "P2"} and d["schedule"]["hour_weights_changed"] == ["hi"]
    assert cl.config_diff(BASE, BASE) == {"changed_dims": []}


def test_allocation_key_order_is_canonical_regardless_of_input_order():
    """The sim samples options in body order: the same weights must always serialise identically (R5)."""
    cfg = copy.deepcopy(BASE)
    cfg["allocations"]["default"]["caller_id"] = {"D3": 0.2, "D1": 0.5, "D2": 0.3}
    other = copy.deepcopy(cfg)
    other["allocations"]["default"]["caller_id"] = {"D2": 0.3, "D3": 0.2, "D1": 0.5}
    a, b = cl.sim_normalise(WF, cfg), cl.sim_normalise(WF, other)
    assert list(a["allocations"]["default"]["caller_id"]) == ["D1", "D2", "D3"]
    assert list(a["allocations"]["default"]["caller_id"]) == list(b["allocations"]["default"]["caller_id"])
