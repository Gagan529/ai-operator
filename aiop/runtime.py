"""The Operator Runtime. It owns the run: state, budgets, loop guard, dispatch.

    propose (LLM) -> resolve target (page) -> policy (deterministic) -> act (browser)
      -> observe -> classify -> recover | continue ... -> "done" -> verify (DB) -> VERIFIED | recover

The LLM only ever proposes. Approvals arrive through `resume()` (called by the
LangGraph interrupt in graph.py, or directly), never through a proposal.
The runtime works without LangGraph: `run_until_blocked()` is a plain loop.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from urllib.parse import urlparse
from typing import Callable

from aiop.assign import assign
from aiop.browser import Browser, BrowserError
from aiop.company import Company
from aiop.evidence import EvidenceStore
from aiop.llm import ActionProposer, Context, ProposalError
from aiop.models import (PAGE_FREE_TOOLS, Decision, Failure, FailureKind, ObservationSummary, Observation, Outcome,
                         PendingApproval, PolicyDecision, ProposedAction, RunState, RunStatus, Target,
                         ToolResult, VerificationResult)
from aiop.policy import PolicyEngine
from aiop.recovery import Move, classify_exception, classify_result, decide
from aiop.verify import Verifier

# Identifier-like tokens: start with a letter, contain a digit (A-203, app203, S11; not 3y, 48.75, 10).
_ID = re.compile(r"\b[A-Za-z][A-Za-z_-]*\d[\w-]*\b")
PAGES_KEPT, PAGE_CHARS, MAX_SEEN_IDS = 3, 3000, 5000


class Runtime:
    def __init__(self, company: Company, browser: Browser, proposer: ActionProposer,
                 evidence: EvidenceStore, sleep: Callable[[float], None] = time.sleep):
        self.company = company
        self.browser = browser
        self.proposer = proposer
        self.evidence = evidence
        self.policy = PolicyEngine(company.policies)
        self.verifier = Verifier(company.db_path, company.invariants)
        self.sleep = sleep
        self._snapshot: str | None = None   # latest full snapshot; never put in the checkpoint
        # Page memory: URL (no query) -> latest snapshot, truncated, most recent last. Also not in the
        # checkpoint: after a resume from a checkpoint it simply starts empty.
        self._pages: dict[str, str] = {}

    # ---------- lifecycle ----------
    @staticmethod
    def new_state(company: Company, task: str | None = None, run_id: str | None = None) -> RunState:
        return RunState(run_id=run_id or time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4],
                        company=company.id, task=task or company.default_task)

    def start(self, state: RunState) -> RunState:
        self.evidence.save_json("db_before.json", self.verifier.snapshot_db())
        self._log(state, "start", f"task: {state.task}", {"company": self.company.name})
        self._observe(state, self.browser.goto(self.company.start_url))
        self._plan(state)
        return state

    def run_until_blocked(self, state: RunState) -> RunState:
        while state.status == RunStatus.RUNNING:
            self.step(state)
        return state

    # ---------- one step ----------
    def step(self, state: RunState) -> RunState:
        if state.step >= state.budgets.max_steps:
            return self._stop(state, RunStatus.STOPPED, f"step budget of {state.budgets.max_steps} used up")
        state.step += 1
        if not state.queue:                  # ask the LLM only when the last batch is used up
            try:
                state.queue = self.proposer.propose_actions(self._ctx(state))
            except (ProposalError, ValueError) as e:
                kind = FailureKind.TRANSIENT if getattr(e, "transient", False) else FailureKind.INVALID_ACTION
                return self._failed(state, Failure(kind=kind, message=str(e)[:300]))
            finally:
                self._tokens(state, "actions")
            state.llm_calls += 1
            state.feedback = None            # the model has now seen it
            batch_size = len(state.queue)
        else:
            batch_size = 0
        action = state.queue.pop(0)
        tag = f"  [batch of {batch_size}]" if batch_size > 1 else ("  [from batch]" if batch_size == 0 else "")
        self._log(state, "propose", _describe(action) + tag, action.model_dump(exclude_none=True))

        if action.tool == "done":
            state.queue = []
            return self._verify(state, claim=action.reason)

        page = state.last_observation.url if state.last_observation else ""
        b = state.budgets
        key = f"{action.key()}@{page}"
        state.recent_keys = (state.recent_keys + [key])[-max(b.loop_window, b.cycle_window):]
        last = state.recent_keys[-b.loop_window:]
        if len(last) == b.loop_window and len(set(last)) == 1:
            return self._stop(state, RunStatus.STOPPED, f"loop guard: the same step on the same page {b.loop_window} times")
        seen = state.recent_keys[-b.cycle_window:].count(key)
        if seen > b.cycle_warn:
            return self._stop(state, RunStatus.STOPPED, f"loop guard: going in circles ({_describe(action)} on {page})")
        if seen == b.cycle_warn:
            return self._failed(state, Failure(kind=FailureKind.INVALID_ACTION, message=(
                f"you are going in circles: '{_describe(action)}' on this page {seen} times in the last "
                f"{b.cycle_window} steps. Use note to save what you need from a page, then move on")))

        target: Target | None = None
        if action.tool == "submit_form":
            # judge the page first, then turn the form into a click on its REAL submit button
            nav = self.policy.decide(ProposedAction(tool="goto", url=action.url))
            if nav.decision == Decision.DENY:
                return self._failed(state, Failure(kind=FailureKind.POLICY_VIOLATION,
                                                   message=f"denied by {nav.rule}: {nav.reason}"))
            try:
                ref = self.browser.prepare_form(action.url or "/", action.fields or {}, action.button)
            except BrowserError as e:
                return self._failed(state, classify_exception(e))
            action = ProposedAction(tool="click", ref=ref, reason=action.reason or f"submit form on {action.url}")
        if action.tool == "click":
            try:
                target = self.browser.resolve(action.ref or "")
            except BrowserError as e:
                return self._failed(state, classify_exception(e))

        decision = self.policy.decide(action, target)
        self._log(state, "policy", f"{decision.decision.value}: {decision.reason}", decision.model_dump())
        if target is not None and target.is_submit and _change(target, decision.effect) in state.denied:
            return self._failed(state, Failure(kind=FailureKind.POLICY_VIOLATION, message=(
                f"a human already DENIED exactly this change ({_change(target, decision.effect)}). A denial is "
                f"final: never propose it again. If the task cannot be finished without it, reply done and say so")))
        if decision.decision == Decision.DENY:
            return self._failed(state, Failure(kind=FailureKind.POLICY_VIOLATION,
                                               message=f"denied by {decision.rule}: {decision.reason}"))
        if decision.decision == Decision.APPROVAL_REQUIRED:
            return self._await_approval(state, action, decision, target)
        if action.tool == "note":
            return self._note(state, action.value or "")
        if action.tool == "assign":
            return self._assign(state, action)
        before = state.last_observation.url if state.last_observation else ""
        ok = self._execute(state, action, target, effect=decision.effect)
        after = state.last_observation.url if state.last_observation else ""
        if not ok:
            state.queue = []
        elif after != before or action.tool == "goto":
            # the model has not seen the new page yet: refs, reads and notes queued now would be blind.
            # Keep only the leading actions that do not depend on it (submit_form, assign).
            cut = next((i for i, a in enumerate(state.queue) if a.tool not in PAGE_FREE_TOOLS), len(state.queue))
            state.queue, dropped = state.queue[:cut], state.queue[cut:]
            if dropped:                      # say so, or the model believes they ran
                said = "; ".join(_describe(a)[:120] for a in dropped)
                self._log(state, "drop", f"dropped {len(dropped)} queued step(s) written before the page changed: {said}",
                          {"dropped": [a.model_dump(exclude_none=True) for a in dropped]})
                state.feedback = (f"{len(dropped)} of your queued steps did NOT run, because you wrote them before "
                                  f"seeing this page: {said}. If they are still needed, propose them again now.")
        return state

    def _note(self, state: RunState, text: str, trusted: bool = False) -> RunState:
        """Working memory: facts the model will need on later pages. Kept small and capped.
        `trusted`: written by the runtime itself (e.g. the assign result), so not checked for invented IDs."""
        b = state.budgets
        text = " ".join(text.split())
        if not text:
            return self._failed(state, Failure(kind=FailureKind.INVALID_ACTION,
                                               message="empty note: write a note after you have seen the page"))
        if not trusted:
            known = f"{state.task}\n{self.company.sop}".lower()
            unseen = [t for t in dict.fromkeys(_ID.findall(text)) if t.lower() not in known
                      and not any(t.lower() in s for s in state.seen_ids)]
            if unseen:
                return self._failed(state, Failure(kind=FailureKind.INVALID_ACTION, message=(
                    f"your note names {', '.join(unseen[:10])}, which no page you have opened shows. "
                    f"Copy values exactly from the pages; do not make them up")))
        cut = len(text) > b.max_note_chars
        text = text[:b.max_note_chars]
        if sum(len(n) for n in state.notes) + len(text) > b.max_notes_chars:
            return self._failed(state, Failure(kind=FailureKind.INVALID_ACTION,
                                               message="notes are full; keep only what you still need"))
        state.notes.append(text)
        state.failures_in_row = 0
        state.recent_keys = []               # progress: revisiting a page is no longer going in circles
        if cut:
            state.feedback = (f"Your note was cut to {b.max_note_chars} characters. Keep notes compact "
                              f"(skills and years, not whole texts); add another note for anything lost.")
        state.history.append(f"{state.step}. noted: {text[:80]}")
        return state

    def _assign(self, state: RunState, action: ProposedAction) -> RunState:
        """Pure computation: solve, keep the full input and output as evidence, put the answer in the notes."""
        try:
            result = assign(action.items or [], action.slots or [])
        except ValueError as e:
            return self._failed(state, Failure(kind=FailureKind.INVALID_ACTION, message=f"assign: {e}"))
        rel = self.evidence.save_json(f"assign-{state.step:03d}.json", {
            "items": [i.model_dump() for i in action.items or []],
            "slots": [s.model_dump() for s in action.slots or []],
            "result": result.model_dump()})
        self._log(state, "assign", result.summary(), result.model_dump(), artifact=rel)
        return self._note(state, result.summary(), trusted=True)

    # ---------- approvals ----------
    def resume(self, state: RunState, approved: bool, by: str = "human", note: str = "") -> RunState:
        """The only way an approval enters the run. Called after the interrupt."""
        pending = state.pending_approval
        if state.status != RunStatus.AWAITING_APPROVAL or pending is None:
            raise RuntimeError("no approval is pending")
        state.pending_approval = None
        state.status = RunStatus.RUNNING
        self._log(state, "approval", f"{'APPROVED' if approved else 'DENIED'} by {by}: {_describe(pending.action)}",
                  {"approved": approved, "by": by, "note": note, "effect": pending.decision.effect})
        if not approved:
            if pending.target.is_submit:
                state.denied.append(_change(pending.target, pending.decision.effect))
            state.feedback = (f"A human DENIED this step ({pending.decision.reason}). This is final: it is now "
                              f"listed under DENIED BY A HUMAN. Continue with the rest of the task.")
            state.history.append(f"{state.step}. {_describe(pending.action)} -> DENIED by {by}")
            self._refresh(state)
            return state
        # The page may have changed (or the process restarted) while waiting: rebuild the
        # form from the recorded target and check the effect is still the approved one.
        target = pending.target
        if target.is_submit:
            ref = self.browser.refill(target)
            fresh = self.browser.resolve(ref)
            again = self.policy.decide(ProposedAction(tool="click", ref=ref), fresh)
            if again.effect != pending.decision.effect:
                return self._failed(state, Failure(kind=FailureKind.AMBIGUOUS_STATE,
                                                   message="the form changed while waiting for approval"))
            target = fresh
            action = ProposedAction(tool="click", ref=ref, reason=pending.action.reason)
        else:
            action = pending.action
        if self._execute(state, action, target, approved_effect=pending.decision.effect,
                         effect=pending.decision.effect):
            if pending.decision.effect:
                state.approved[pending.decision.effect] = state.approved.get(pending.decision.effect, 0) + 1
        return state

    def _await_approval(self, state: RunState, action: ProposedAction, decision: PolicyDecision,
                        target: Target | None) -> RunState:
        state.queue = []                     # the model re-plans after the human answers
        state.pending_approval = PendingApproval(action=action, decision=decision,
                                                 target=target or Target(ref=action.ref or "", tag=""))
        state.status = RunStatus.AWAITING_APPROVAL
        shot = f"screenshots/{state.step:03d}-approval.png"
        self.browser.screenshot(self.evidence.path(shot))
        self._log(state, "approval_request", f"needs approval: {decision.reason}",
                  {"effect": decision.effect, "rule": decision.rule,
                   "fields": target.form_fields if target else {}}, artifact=shot)
        return state

    # ---------- acting, with deterministic retries ----------
    def _execute(self, state: RunState, action: ProposedAction, target: Target | None,
                 approved_effect: str | None = None, effect: str | None = None) -> bool:
        attempt = 0
        while True:
            try:
                result = self._call(action, target, attempt, approved_effect)
                failure = classify_result(result)
            except BrowserError as e:
                result, failure = None, classify_exception(e)
            if result is not None:
                self._observe(state, result)
            if failure is None:
                state.failures_in_row = 0
                state.history.append(f"{state.step}. {_describe(action)} -> ok, now at {result.observation.url}")
                if target is not None and target.is_submit:
                    state.completed.append(_change(target, effect))
                    state.recent_keys = []   # progress resets the cycle guard
                return True
            self._log(state, "failure", f"{failure.kind.value}: {failure.message}", failure.model_dump(mode="json"))
            retryable = action.tool == "goto" or (target is not None and (target.is_submit or bool(target.href)))
            rec = decide(failure, state, attempt, retryable)
            if rec.move != Move.RETRY:
                self._failed(state, failure, logged=True)
                return False
            attempt += 1
            state.recoveries += 1
            self._log(state, "recover", f"retry {attempt}/{state.budgets.max_retries_per_step} "
                                        f"after {rec.delay_s:.1f}s: {rec.hint}")
            self.sleep(rec.delay_s)

    def _call(self, action: ProposedAction, target: Target | None, attempt: int,
              approved_effect: str | None) -> ToolResult:
        b = self.browser
        if attempt and target is not None and target.is_submit:
            # retry a submit by rebuilding the form; the policy re-checks the fresh control
            ref = b.refill(target)
            d = self.policy.decide(ProposedAction(tool="click", ref=ref), b.resolve(ref))
            if d.decision == Decision.DENY or (d.decision == Decision.APPROVAL_REQUIRED and d.effect != approved_effect):
                raise BrowserError(f"retry refused: policy now says {d.decision.value}")
            return b.click(ref)
        if attempt and target is not None and target.href:
            return b.goto(target.href)
        if action.tool == "goto":
            return b.goto(action.url or "/")
        if action.tool == "click":
            return b.click(action.ref or "")
        if action.tool == "fill":
            return b.fill(action.ref or "", action.value or "")
        if action.tool == "select":
            return b.select(action.ref or "", action.value or "")
        return b.read()

    # ---------- verification ----------
    def _check(self, state: RunState, said: str) -> VerificationResult:
        """Run the data checks, keep them as evidence and log the verdict."""
        result = self.verifier.run({"approved": state.approved}, self.company.goal_checks(state.task))
        state.verification = result.outcome
        state.verification_ref = self.evidence.save_json(f"verification-{state.step:03d}.json",
                                                         result.model_dump(mode="json"))
        failed = [c.name for c in result.checks if not c.passed]
        self._log(state, "verify", f"{said} {result.outcome.value}"
                  + (f": {len(failed)} check(s) failed" if failed else ""),
                  {"outcome": result.outcome.value, "failed": failed}, artifact=state.verification_ref)
        return result

    def _verify(self, state: RunState, claim: str) -> RunState:
        result = self._check(state, f"agent claimed done ({claim!r}); verifier says")
        if result.outcome == Outcome.PASS:
            return self._finish(state, RunStatus.VERIFIED, "all verification checks passed", result)
        if result.outcome == Outcome.UNVERIFIED:
            return self._finish(state, RunStatus.UNVERIFIED, "invariants hold, but this task has no goal checks, "
                                "so the result is not machine-verified; see the evidence", result)
        detail = "; ".join(f"{c.name} (expected {c.expected}, found {c.actual})"
                           for c in result.checks if not c.passed)
        if result.outcome == Outcome.PARTIAL:
            return self._finish(state, RunStatus.VERIFIED, f"verified with warnings: {detail}", result)
        return self._failed(state, Failure(kind=FailureKind.VERIFICATION_FAILURE,
                                           message=f"verification {result.outcome.value}: {detail}"),
                            result=result)

    # ---------- failure handling ----------
    def _failed(self, state: RunState, f: Failure, logged: bool = False,
                result: VerificationResult | None = None) -> RunState:
        if not logged:
            self._log(state, "failure", f"{f.kind.value}: {f.message}", f.model_dump(mode="json"))
        state.queue = []
        rec = decide(f, state, attempt=0, retryable=False)
        state.failures_in_row += 1
        if rec.move == Move.STOP:
            status = RunStatus.FAILED if f.kind == FailureKind.VERIFICATION_FAILURE else RunStatus.STOPPED
            return self._finish(state, status, f"{rec.hint}; last failure: {f.message}", result)
        state.feedback = f"{f.kind.value}: {f.message}. {rec.hint}."
        self._refresh(state)
        if rec.move == Move.REPLAN:
            state.replans += 1
            state.failures_in_row = 0
            self._log(state, "recover", f"re-plan {state.replans}/{state.budgets.max_replans}: {rec.hint}")
            self._plan(state)
        else:
            self._log(state, "recover", f"back to the LLM with feedback: {rec.hint}")
        return state

    def _stop(self, state: RunState, status: RunStatus, reason: str) -> RunState:
        self._log(state, "stop", reason)
        return self._finish(state, status, reason, None)

    def _finish(self, state: RunState, status: RunStatus, reason: str,
                result: VerificationResult | None) -> RunState:
        state.status = status
        state.stop_reason = reason
        state.queue = []
        if result is None and state.verification is None:
            # The agent never said done, but the report must still tell the truth about the data.
            result = self._check(state, "run ended before the agent said done; data check says")
            reason = f"{reason} (data check: {result.outcome.value})"
            state.stop_reason = reason
        after = self.verifier.snapshot_db()
        self.evidence.save_json("db_after.json", after)
        before_path = self.evidence.dir / "db_before.json"
        if before_path.exists():
            self.evidence.save_json("db_diff.json",
                                    self.evidence.db_diff(json.loads(before_path.read_text(encoding="utf-8")), after))
        try:
            self.browser.screenshot(self.evidence.path("screenshots/final.png"))
        except Exception as e:  # a missing final screenshot must not hide the result
            self._log(state, "note", f"final screenshot failed: {e}")
        self._log(state, "end", f"{status.value}: {reason}")
        self.evidence.write_report(state, result)
        return state

    # ---------- helpers ----------
    def _plan(self, state: RunState) -> None:
        try:
            plan = self.proposer.propose_plan(self._ctx(state))
        except (ProposalError, ValueError) as e:
            self._log(state, "plan", f"planning failed, continuing without a plan: {e}")
            return
        finally:
            self._tokens(state, "plan")
        state.plan = plan.steps
        self._log(state, "plan", f"{len(plan.steps)} steps", {"steps": plan.steps})

    def _observe(self, state: RunState, result: ToolResult) -> None:
        obs: Observation = result.observation
        self._snapshot = obs.snapshot
        key = obs.url.split("?", 1)[0]
        self._pages.pop(key, None)           # re-insert: most recently visited last
        snap = obs.snapshot
        self._pages[key] = snap if len(snap) <= PAGE_CHARS else snap[:PAGE_CHARS] + "… (truncated)"
        for old in list(self._pages)[:-(PAGES_KEPT + 1)]:   # the current page plus 3 others
            del self._pages[old]
        new = {t.lower() for t in _ID.findall(snap)} - set(state.seen_ids)
        room = MAX_SEEN_IDS - len(state.seen_ids)
        if new and room > 0:                 # first seen are kept; the cap bounds the checkpoint size
            state.seen_ids = sorted(set(state.seen_ids) | set(sorted(new)[:room]))
        rel = self.evidence.save_text(f"observations/{state.step:03d}-{len(state.event_ids):04d}.txt", obs.snapshot)
        state.last_observation = ObservationSummary(url=obs.url, title=obs.title, http_status=obs.http_status,
                                                    error_text=obs.error_text, evidence_ref=rel)
        self._log(state, "observe", f"{obs.http_status} {obs.title}", {"url": obs.url}, artifact=rel)

    def _refresh(self, state: RunState) -> None:
        try:
            self._observe(state, self.browser.read())
        except BrowserError as e:
            self._log(state, "note", f"could not re-read the page: {e}")

    def _ctx(self, state: RunState) -> Context:
        if self._snapshot is None:           # e.g. after resuming from a checkpoint
            self._snapshot = self.browser.read().observation.snapshot
        here = state.last_observation.url.split("?", 1)[0] if state.last_observation else None
        pages = [f"{url}\n{snap}" for url, snap in self._pages.items() if url != here][-PAGES_KEPT:]
        return Context(task=state.task, sop=self.company.sop, snapshot=self._snapshot, plan=state.plan,
                       history=state.history, feedback=state.feedback, notes=state.notes,
                       completed=state.completed, denied=state.denied, pages=pages)

    def _tokens(self, state: RunState, kind: str) -> None:
        p = self.proposer
        state.tokens_in, state.tokens_out = p.tokens_in, p.tokens_out
        state.tokens_cached = getattr(p, "tokens_cached", 0)
        state.tokens_reasoning = getattr(p, "tokens_reasoning", 0)
        usage = getattr(p, "last_usage", None)
        if usage:                            # one line per LLM call, for the cost and caching numbers
            self.evidence.append_jsonl("llm_usage.jsonl", {"step": state.step, "kind": kind, **usage})
            p.last_usage = None

    def _log(self, state: RunState, kind: str, summary: str, data: dict | None = None,
             artifact: str | None = None) -> None:
        state.event_ids.append(self.evidence.add(state.step, kind, summary, data, artifact))


def _change(t: Target, effect: str | None) -> str:
    """One line for the ALREADY COMPLETED list, e.g. 'approve_record @ /records/R-1 (state=...)'."""
    fields = ", ".join(f"{k}={v[:30]}" for k, v in t.form_fields.items() if v)
    return f"{effect or 'submit'} @ {urlparse(t.form_action or t.page_url).path}" + (f" ({fields})" if fields else "")


def _describe(a: ProposedAction) -> str:
    arg = a.url or a.ref or ""
    val = f' = "{a.value}"' if a.value is not None else ""
    return f"{a.tool} {arg}{val}".strip() + (f"  ({a.reason})" if a.reason else "")
