"""Proposal drafts, local validation (P7) and the C10 fields.

A proposal that fails local validation is never sent: the approver does not error on soft
problems (it creates the request, burns a fatigue slot and rejects it later), and the
auto-apply endpoint logs a graded refusal. So every check the sim makes is repeated here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from . import dryrun
from .cohorts import FeatureVocab
from .configlib import (  # noqa: F401  (re-exported: the builder lives in configlib)
    AGENT_DIMS, ProposalError, apply_patch, changed_dims, config_diff, max_shift, tv_by_cohort)
from .desired import DesiredChange

_EPS = 1e-9
ValidationLookup = Callable[[str], Mapping | None]


@dataclass(frozen=True)
class ProposalDraft:
    workflow_id: str
    kind: str
    base_version: int
    proposed_config: dict
    dims: tuple[str, ...]
    max_shift: float
    tv: dict  # {"cohort/dim": tv}
    change_summary: str
    diff: dict
    affected_leads: int
    dry_run: dict
    impact_score: float
    rationale: str
    evidence: dict = field(default_factory=dict)
    expires_in_hours: float | None = None

    def approval_body(self, proposal_id: str) -> dict:
        body = {"proposal_id": proposal_id, "workflow_id": self.workflow_id,
                "base_version": self.base_version, "proposed_config": self.proposed_config,
                "change_summary": self.change_summary, "diff": self.diff,
                "affected_leads": self.affected_leads, "dry_run": self.dry_run,
                "impact_score": self.impact_score, "rationale": self.rationale}
        if self.evidence:
            body["evidence"] = self.evidence
        if self.expires_in_hours is not None:
            body["expires_in_hours"] = self.expires_in_hours
        return body

    def auto_body(self, proposal_id: str) -> dict:
        body = {"proposal_id": proposal_id, "base_version": self.base_version,
                "proposed_config": self.proposed_config, "rationale": self.rationale}
        if self.evidence:
            body["evidence"] = self.evidence
        return body


def validate_approval_fields(draft: ProposalDraft, wf: Mapping,
                             lookup_validation: ValidationLookup) -> list[str]:
    """Every soft check the approver makes at decision time (docs/SIMULATOR.md 4.1)."""
    problems: list[str] = []
    if not draft.dims:
        problems.append("no change: proposed config equals the current config")
    if not str(draft.change_summary or "").strip():
        problems.append("change_summary is empty")
    if not draft.diff:
        problems.append("diff is empty")
    if not draft.dry_run:
        problems.append("dry_run is empty")
    if not str(draft.rationale or "").strip():
        problems.append("rationale is empty")
    n = draft.affected_leads
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        problems.append("affected_leads must be a non-negative integer")
    s = draft.impact_score
    if isinstance(s, bool) or not isinstance(s, (int, float)) or not 0 <= s <= 1:
        problems.append("impact_score must be in [0,1]")
    if "prompt_version" in draft.dims:
        run_id = str((draft.evidence or {}).get("validation_run_id") or "")
        run = lookup_validation(run_id) if run_id else None
        need = wf["governance"]["min_validation_pass_rate"]
        if (not run or run.get("workflow_id") != draft.workflow_id
                or run.get("prompt_version") != draft.proposed_config["prompt_version"]):
            problems.append("prompt change needs evidence.validation_run_id for this workflow and prompt")
        elif run["pass_rate"] < need:
            problems.append(f"validation pass rate {run['pass_rate']:.3f} below {need}")
    return problems


def validate_auto_apply(draft: ProposalDraft, wf: Mapping) -> list[str]:
    """The auto-apply policy: enabled, dims allowed, TV <= max_shift, rationale, real change."""
    pol = wf["governance"]["auto_apply"]
    problems: list[str] = []
    if not pol.get("enabled"):
        problems.append("auto-apply is not enabled for this workflow")
    bad = [d for d in draft.dims if d not in pol.get("allowed_dims", [])]
    if bad:
        problems.append(f"dims not permitted for auto-apply: {bad}")
    if "cohorts" in draft.dims:  # defense in depth: cohort definitions always need approval (README)
        problems.append("cohort definitions are never auto-applied: dims not permitted for auto-apply: ['cohorts']")
    if draft.max_shift > pol.get("max_shift", 0) + _EPS:
        problems.append(f"allocation shift {draft.max_shift:.3f} exceeds max_shift {pol.get('max_shift', 0)}")
    if not draft.dims:
        problems.append("no change")
    if not str(draft.rationale or "").strip():
        problems.append("rationale is required")
    return problems


def _summary(changes: Sequence[DesiredChange], dims: Sequence[str]) -> str:
    parts = [c.summary.strip() for c in changes if c.summary.strip()]
    text = "; ".join(dict.fromkeys(parts)) or f"{changes[0].kind}: change {', '.join(dims)}"
    return text[:300]


def _rationale(changes: Sequence[DesiredChange]) -> str:
    parts = [str(c.evidence.get("rationale") or c.summary or "").strip() for c in changes]
    return " | ".join(dict.fromkeys(p for p in parts if p))


def build_draft(store, vocab: FeatureVocab, wf: Mapping, version: int, current: Mapping,
                changes: Sequence[DesiredChange], now: int,
                expires_in_hours: float | None = None) -> ProposalDraft:
    """Merge the changes (least urgent first, so the most urgent wins) into a FULL config."""
    cfg = dict(current)
    for change in sorted(changes, key=lambda c: -c.priority):
        cfg = apply_patch(wf, cfg, change.patch, vocab)
    dims = tuple(changed_dims(current, cfg))
    evidence: dict = {}
    for change in changes:
        if change.evidence.get("validation_run_id"):
            evidence["validation_run_id"] = change.evidence["validation_run_id"]
    tv = tv_by_cohort(current, cfg)
    dry = dryrun.build_dry_run(store, vocab, wf, current, cfg, now, list(dims), evidence)
    for change in changes:  # producers may attach their own estimates (e.g. the call-time guardrail dry run)
        dry.update(change.evidence.get("dry_run_extra", {}))
    overrides = [c.evidence["impact_override"] for c in changes if "impact_override" in c.evidence]
    override = max(overrides) if overrides else None
    return ProposalDraft(
        workflow_id=wf["workflow_id"], kind=min(changes, key=lambda c: (c.priority, c.kind)).kind,
        base_version=version, proposed_config=cfg, dims=dims, max_shift=max(tv.values(), default=0.0),
        tv={f"{k[0]}/{k[1]}": round(v, 6) for k, v in tv.items() if v > 0},
        change_summary=_summary(changes, dims), diff=config_diff(current, cfg),
        affected_leads=dryrun.affected_leads(store, vocab, wf, current, cfg, now), dry_run=dry,
        impact_score=dryrun.impact_score(dry, wf["objective"]["primary"], override),
        rationale=_rationale(changes), evidence=evidence, expires_in_hours=expires_in_hours)
