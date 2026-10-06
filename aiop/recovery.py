"""Failure classification and the recovery move for each kind.

ACT -> OBSERVE -> EVALUATE -> classify -> move:
  TRANSIENT                      -> RETRY the same step (deterministic, with backoff), up to the cap
  MISSING_ELEMENT / PAGE_CHANGED -> re-observe, then ASK the LLM with feedback
  INVALID_ACTION / APP_ERROR     -> ASK the LLM with feedback
  POLICY_VIOLATION               -> ASK the LLM with the denial reason
  VERIFICATION_FAILURE           -> REPLAN with the failed checks (never report success)
Too many failures in a row -> REPLAN; replans used up -> STOP. Nothing retries forever.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from aiop.browser import ActionFailed, BrowserError, DomainNotAllowed, MissingElement
from aiop.models import Failure, FailureKind, RunState, ToolResult

MAX_FAILURES_IN_ROW = 3


class Move(str, Enum):
    RETRY = "RETRY"
    ASK_LLM = "ASK_LLM"
    REPLAN = "REPLAN"
    STOP = "STOP"


@dataclass
class Recovery:
    move: Move
    hint: str
    delay_s: float = 0.0


def classify_exception(e: Exception) -> Failure:
    if isinstance(e, MissingElement):
        return Failure(kind=FailureKind.MISSING_ELEMENT, message=str(e))
    if isinstance(e, DomainNotAllowed):
        return Failure(kind=FailureKind.POLICY_VIOLATION, message=str(e))
    if isinstance(e, (ActionFailed, BrowserError)):
        return Failure(kind=FailureKind.INVALID_ACTION, message=str(e))
    return Failure(kind=FailureKind.INVALID_ACTION, message=f"{type(e).__name__}: {e}")


def classify_result(r: ToolResult) -> Failure | None:
    """None means the step worked, as far as the page can tell."""
    obs = r.observation
    if not r.ok:
        err = r.error or "action failed"
        kind = FailureKind.TRANSIENT if "Timeout" in err else FailureKind.INVALID_ACTION
        return Failure(kind=kind, message=err)
    if obs.http_status is not None and obs.http_status >= 500:
        return Failure(kind=FailureKind.TRANSIENT,
                       message=f"HTTP {obs.http_status}: {obs.error_text or obs.title}")
    if obs.http_status is not None and obs.http_status >= 400:
        return Failure(kind=FailureKind.APP_ERROR, message=f"HTTP {obs.http_status}: {obs.error_text or obs.title}")
    if obs.error_text:
        return Failure(kind=FailureKind.APP_ERROR, message=obs.error_text)
    return None


def decide(f: Failure, state: RunState, attempt: int, retryable: bool) -> Recovery:
    b = state.budgets
    if f.kind == FailureKind.TRANSIENT and retryable and attempt < b.max_retries_per_step:
        return Recovery(Move.RETRY, "transient failure; retrying the same step", delay_s=0.5 * 2 ** attempt)
    if f.kind == FailureKind.VERIFICATION_FAILURE or state.failures_in_row + 1 >= MAX_FAILURES_IN_ROW:
        if state.replans < b.max_replans:
            return Recovery(Move.REPLAN, "re-planning from the current state")
        return Recovery(Move.STOP, "recovery budget used up (re-plans exhausted)")
    hints = {
        FailureKind.TRANSIENT: "the app kept failing; try a different way or re-check the page",
        FailureKind.MISSING_ELEMENT: "that ref is not on the page; use a ref from the latest snapshot",
        FailureKind.PAGE_CHANGED: "the page is not what was expected; re-read it",
        FailureKind.INVALID_ACTION: "that step could not be performed; propose a valid one",
        FailureKind.APP_ERROR: "the app rejected the step; read its message and adjust",
        FailureKind.POLICY_VIOLATION: "company policy blocks that step; choose another way that follows the SOP",
        FailureKind.AMBIGUOUS_STATE: "unclear whether the step took effect; re-read before acting again",
    }
    return Recovery(Move.ASK_LLM, hints.get(f.kind, "propose a different step"))
