"""Typed data passed between the operator's parts.

Two rules shape these types:
- What the LLM may say is narrow. `ProposedAction` and `Plan` forbid extra keys,
  so a model cannot smuggle in an "effect", "approved" or "skip_policy" field.
- What the checkpoint stores is small. `RunState` keeps summaries and evidence
  IDs; full snapshots, screenshots and DB dumps live in the evidence store.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------- What the LLM may propose ----------
Tool = Literal["goto", "click", "fill", "select", "read", "note", "done"]


class ProposedAction(BaseModel):
    """One step proposed by the LLM. The runtime decides whether it runs."""
    model_config = ConfigDict(extra="forbid")

    tool: Tool
    ref: str | None = Field(None, description="data-aiop-ref of the target element (click/fill/select)")
    url: str | None = Field(None, description="absolute or site-relative URL (goto)")
    value: str | None = Field(None, description="text to type (fill), option label (select) or fact to remember (note)")
    reason: str = Field("", description="one line: why this step, or the summary when tool=done")

    @model_validator(mode="after")
    def _needs_args(self) -> ProposedAction:
        need = {"goto": ["url"], "click": ["ref"], "fill": ["ref", "value"], "select": ["ref", "value"],
                "note": ["value"]}
        missing = [f for f in need.get(self.tool, []) if getattr(self, f) is None]
        if missing:
            raise ValueError(f"{self.tool} needs {', '.join(missing)}")
        return self

    def key(self) -> str:
        """Stable identity used by the loop guard."""
        return f"{self.tool}|{self.ref}|{self.url}|{self.value}"


class ActionBatch(BaseModel):
    """Up to 5 actions in one reply (e.g. fill, fill, click Book). The runtime still checks and runs
    them one at a time, and drops the rest after a failure, an approval request or a page change."""
    model_config = ConfigDict(extra="forbid")
    actions: list[ProposedAction] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def _done_alone(self) -> ActionBatch:
        if len(self.actions) > 1 and any(a.tool == "done" for a in self.actions):
            raise ValueError("done must be the only action in its reply")
        return self


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[str]


# ---------- What the runtime observes ----------
class Target(BaseModel):
    """The real element behind a ref, resolved by the browser, never by the LLM."""
    ref: str
    tag: str
    label: str = ""
    effect: str | None = None              # data-aiop-effect on the element itself
    page_url: str = ""
    href: str | None = None                # for links: absolute destination
    is_submit: bool = False
    form_action: str | None = None         # for submit controls: where the form posts
    form_method: str | None = None
    form_fields: dict[str, str] = {}       # field name -> value at submit time
    form_effects: list[str] = []           # effects of selected options in that form


class Observation(BaseModel):
    """A full page observation. Stored in the evidence store, not in the checkpoint."""
    url: str
    title: str
    http_status: int | None
    error_text: str | None
    snapshot: str                          # compact text the LLM sees
    ts: str = Field(default_factory=now)


class ObservationSummary(BaseModel):
    """What the checkpoint keeps about the last observation."""
    url: str
    title: str
    http_status: int | None
    error_text: str | None
    evidence_ref: str | None = None        # path of the full snapshot in runs/<id>/


class ToolResult(BaseModel):
    ok: bool
    observation: Observation
    error: str | None = None               # exception text, if the tool itself failed


# ---------- Policy ----------
class Decision(str, Enum):
    ALLOW = "ALLOW"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    DENY = "DENY"


class PolicyDecision(BaseModel):
    decision: Decision
    effect: str | None
    rule: str                              # which rule matched, for the audit trail
    reason: str


# ---------- Failures and recovery ----------
class FailureKind(str, Enum):
    TRANSIENT = "TRANSIENT"                # 5xx, timeouts: retry with backoff
    MISSING_ELEMENT = "MISSING_ELEMENT"    # ref not on the page: refresh and re-resolve
    PAGE_CHANGED = "PAGE_CHANGED"          # landed somewhere unexpected
    INVALID_ACTION = "INVALID_ACTION"      # malformed or impossible proposal
    APP_ERROR = "APP_ERROR"                # the app said no (4xx, error text)
    POLICY_VIOLATION = "POLICY_VIOLATION"  # policy denied the action
    AMBIGUOUS_STATE = "AMBIGUOUS_STATE"    # cannot tell whether the action took effect
    VERIFICATION_FAILURE = "VERIFICATION_FAILURE"  # agent said done, verifier disagrees


class Failure(BaseModel):
    kind: FailureKind
    message: str


# ---------- Verification ----------
class VerificationCheck(BaseModel):
    name: str
    expected: Any
    actual: Any
    passed: bool
    critical: bool = True                  # a failed critical check makes the run FAIL
    evidence: str = ""                     # the query or source that produced `actual`


class Outcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    PARTIAL = "PARTIAL"


class VerificationResult(BaseModel):
    outcome: Outcome
    checks: list[VerificationCheck]

    @classmethod
    def from_checks(cls, checks: list[VerificationCheck]) -> VerificationResult:
        """PASS: all passed. FAIL: a critical check failed. PARTIAL: only non-critical ones failed."""
        if all(c.passed for c in checks):
            outcome = Outcome.PASS
        elif any(c.critical and not c.passed for c in checks):
            outcome = Outcome.FAIL
        else:
            outcome = Outcome.PARTIAL
        return cls(outcome=outcome, checks=checks)


# ---------- Evidence ----------
class EvidenceEvent(BaseModel):
    id: str
    ts: str = Field(default_factory=now)
    step: int
    kind: str                              # plan / propose / policy / approval / act / observe / failure / recover / verify / stop
    summary: str
    data: dict[str, Any] = {}
    artifact: str | None = None            # relative path inside runs/<id>/


# ---------- Run state (the checkpoint) ----------
class RunStatus(str, Enum):
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    VERIFIED = "VERIFIED"                  # agent finished AND the verifier passed
    FAILED = "FAILED"                      # verifier failed or recovery gave up
    STOPPED = "STOPPED"                    # budget, loop guard or human deny


class PendingApproval(BaseModel):
    action: ProposedAction
    decision: PolicyDecision
    target: Target


class Budgets(BaseModel):
    max_steps: int = 60
    max_retries_per_step: int = 3
    max_replans: int = 2
    loop_window: int = 3                   # same action + same page this many times in a row -> stop
    cycle_window: int = 8                  # cycle guard looks at this many recent steps:
    cycle_warn: int = 3                    #   a step seen this often -> refuse it once, with a warning
                                           #   seen once more after that -> stop
    max_note_chars: int = 1000             # per note (one note may cover a whole table)
    max_notes_chars: int = 6000            # all notes together


class RunState(BaseModel):
    """Small by design: no snapshots, screenshots or DB dumps, only references."""
    run_id: str
    company: str
    task: str
    plan: list[str] = []
    step: int = 0
    status: RunStatus = RunStatus.RUNNING
    pending_action: ProposedAction | None = None
    queue: list[ProposedAction] = []       # rest of the current batch, still to be checked and run
    completed: list[str] = []              # business changes the runtime saw succeed (shown as "don't repeat")
    llm_calls: int = 0
    pending_approval: PendingApproval | None = None
    last_observation: ObservationSummary | None = None
    verification: Outcome | None = None
    verification_ref: str | None = None
    event_ids: list[str] = []
    history: list[str] = []                # one line per executed step, shown to the proposer
    notes: list[str] = []                  # the operator's working memory (the `note` tool), capped
    feedback: str | None = None            # runtime -> proposer note about the last proposal
    approved: dict[str, int] = {}          # effect -> number of human-approved actions
    failures_in_row: int = 0
    recoveries: int = 0
    replans: int = 0
    recent_keys: list[str] = []            # loop guard: last few action+page keys
    stop_reason: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    budgets: Budgets = Budgets()
