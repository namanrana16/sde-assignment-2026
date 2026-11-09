"""Approval texts built from settings: every placeholder is interpolated (no raw "{...}" reaches the approver)."""
from controller import exp_proposals as xp, settings

REC = {"lift": -0.12, "z": -3.4, "n_arm": 210, "n_ctrl": 900, "boundary": 2.1, "recent_lb": 0.01,
       "pickup": {"drop_upper": 0.004, "margin": 0.02}}
W = {"V1": 0.775, "V2": 0.075, "V3": 0.075, "V4": 0.075}


def _all_texts():
    yield xp.entry("voice", "V1", W, "")
    yield xp.update("voice", W, ["V2"], ["V3"], {"V2": REC, "V3": dict(REC, recent_z=-1.6)})
    yield xp.promotion("voice", "workflow", "V2", "V1", REC, {"V2": 0.5, "V1": 0.5})
    yield xp.stage2("voice", "V2", "recent lower bound positive", {"V2": 0.9, "V1": 0.1})
    yield xp.harm_stop("voice", "V3", REC)
    yield xp.rollback("voice", "V2", "V1", "sequential alarm")


def test_no_approval_text_contains_an_uninterpolated_placeholder():
    for parts in _all_texts():
        for text in parts:
            if isinstance(text, str):
                assert "{" not in text and "}" not in text, text


def test_harm_stop_rationale_names_the_check_interval_from_settings():
    _, rationale = xp.harm_stop("voice", "V3", REC)
    assert f"{settings.EXP_HARM_CHECK_MINUTES / 60:g}-hourly check" in rationale
