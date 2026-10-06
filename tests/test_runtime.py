"""The runtime end to end: real browser, real app, scripted proposer."""
import json
import sqlite3

from aiop.models import Outcome, ProposedAction as A, RunStatus
from mockapps import recruiting
from tests.scripts import DONE, screening, set_status


def status_of(cid):
    con = sqlite3.connect(recruiting.DB)
    try:
        return con.execute("SELECT status FROM candidates WHERE id=?", (cid,)).fetchone()[0]
    finally:
        con.close()


def test_snapshot_shows_refs_and_effects(browser, company):
    snap = browser.goto("/ats/candidate/A-101").observation.snapshot
    assert "Rejected {effect=reject_candidate}" in snap and "[e" in snap
    snap = browser.goto("/calendar").observation.snapshot
    assert 'button "Book" (submit) effect=book_interview' in snap


def test_happy_path_verified(make_runtime):
    rt, st = make_runtime(screening() + [DONE])
    rt.run_until_blocked(rt.start(st))
    assert st.status == RunStatus.VERIFIED and st.verification == Outcome.PASS
    diff = json.loads((rt.evidence.dir / "db_diff.json").read_text())
    assert diff["slots"]["S-1"]["after"]["candidate"] == "A-105"
    assert (rt.evidence.dir / "report.html").exists()


def test_rejection_waits_for_human_then_runs(make_runtime):
    rt, st = make_runtime(screening() + set_status("A-106", "Rejected", "no experience") + [DONE])
    rt.run_until_blocked(rt.start(st))
    assert st.status == RunStatus.AWAITING_APPROVAL
    assert st.pending_approval.decision.effect == "reject_candidate"
    assert status_of("A-106") == "Not a fit (pending approval)"      # nothing happened yet
    rt.resume(st, approved=True, by="recruiter")
    assert status_of("A-106") == "Rejected"
    rt.run_until_blocked(st)
    assert st.status == RunStatus.VERIFIED and st.approved == {"reject_candidate": 1}


def test_denied_rejection_is_not_performed(make_runtime):
    rt, st = make_runtime(screening() + set_status("A-106", "Rejected") + [DONE])
    rt.run_until_blocked(rt.start(st))
    rt.resume(st, approved=False, by="recruiter")
    assert "DENIED" in st.feedback
    rt.run_until_blocked(st)
    assert status_of("A-106") == "Not a fit (pending approval)"
    assert st.status == RunStatus.VERIFIED


def test_flaky_503_is_retried(make_runtime):
    recruiting.load_faults(flaky=True)
    rt, st = make_runtime(screening() + [DONE])
    rt.run_until_blocked(rt.start(st))
    events = rt.evidence.events()
    assert any(e.kind == "failure" and e.summary.startswith("TRANSIENT: HTTP 503") for e in events)
    assert any(e.kind == "recover" and e.summary.startswith("retry 1/") for e in events)
    assert st.status == RunStatus.VERIFIED


def test_silent_fail_is_caught(make_runtime):
    recruiting.load_faults(silent_fail=True)
    rt, st = make_runtime(screening() + [DONE])
    rt.run_until_blocked(rt.start(st))
    assert st.status == RunStatus.FAILED and st.verification == Outcome.FAIL
    verdicts = [e for e in rt.evidence.events() if e.kind == "verify"]
    assert verdicts and all("agent claimed done" in e.summary for e in verdicts)


def test_denied_navigation_goes_back_to_llm(make_runtime):
    rt, st = make_runtime([A(tool="goto", url="/admin/reset")] + screening() + [DONE])
    rt.run_until_blocked(rt.start(st))
    assert any(e.kind == "policy" and e.summary.startswith("DENY") for e in rt.evidence.events())
    assert st.status == RunStatus.VERIFIED


def test_loop_guard_stops(make_runtime):
    rt, st = make_runtime([A(tool="read")] * 10)
    rt.run_until_blocked(rt.start(st))
    assert st.status == RunStatus.STOPPED and "loop guard" in st.stop_reason


def test_missing_ref_is_recovered(make_runtime):
    rt, st = make_runtime([A(tool="click", ref="e999")] + screening() + [DONE])
    rt.run_until_blocked(rt.start(st))
    assert any(e.summary.startswith("MISSING_ELEMENT") for e in rt.evidence.events())
    assert st.status == RunStatus.VERIFIED


def test_checkpoint_state_is_small(make_runtime):
    rt, st = make_runtime(screening() + set_status("A-106", "Rejected") + [DONE])
    rt.run_until_blocked(rt.start(st))
    blob = st.model_dump_json()
    assert st.status == RunStatus.AWAITING_APPROVAL
    assert len(blob) < 20_000, len(blob)
    assert "URL: http" not in blob and ".png" not in blob     # no snapshots or screenshots inline
    assert st.last_observation.evidence_ref.startswith("observations/")


def test_notes_are_working_memory(make_runtime):
    rt, st = make_runtime([A(tool="note", value="R-12 needs Python, SQL, REST APIs; 2+ years"),
                           A(tool="note", value="x" * 5000)] + screening() + [DONE])
    rt.start(st)
    rt.step(st)
    assert st.notes == ["R-12 needs Python, SQL, REST APIs; 2+ years"]
    assert "R-12 needs Python" in rt._ctx(st).notes[0]          # shown to the model on later steps
    rt.step(st)                                                 # an oversized note is capped, not stored whole
    assert len(st.notes[1]) == st.budgets.max_note_chars
    rt.run_until_blocked(st)
    assert st.status == RunStatus.VERIFIED


def test_cycle_guard_warns_then_stops(make_runtime):
    bounce = [A(tool="goto", url="/ats/role/R-12"), A(tool="goto", url="/ats")] * 6
    rt, st = make_runtime(bounce)
    rt.run_until_blocked(rt.start(st))
    warnings = [e for e in rt.evidence.events() if e.kind == "failure" and "going in circles" in e.summary]
    assert warnings                                             # the model was told first
    assert st.status == RunStatus.STOPPED and "going in circles" in st.stop_reason


def test_table_rows_appear_once(browser, company):
    snap = browser.goto("/jobs").observation.snapshot
    assert snap.count("Rohan Mehta") == 1
    assert "| A-101 | Rohan Mehta |" in snap


# ---------- batched actions ----------
from aiop.llm import find_ref  # noqa: E402


def save_status_batch(cid, status):
    """One LLM reply on the candidate page: select + fill + click Save."""
    return [A(tool="goto", url=f"/ats/candidate/{cid}"),
            lambda s: [A(tool="select", ref=find_ref(s, r'select "Status"'), value=status),
                       A(tool="fill", ref=find_ref(s, r'textbox "Note"'), value="batched"),
                       A(tool="click", ref=find_ref(s, r'button "Save"'))]]


def book_batch(slot, cid):
    return [A(tool="goto", url="/calendar"),
            lambda s: [A(tool="fill", ref=find_ref(s, r'textbox "Slot ID"'), value=slot),
                       A(tool="fill", ref=find_ref(s, r'textbox "Candidate ID"'), value=cid),
                       A(tool="click", ref=find_ref(s, r'button "Book"'))]]


def test_batch_runs_each_action_through_policy(make_runtime):
    rt, st = make_runtime(save_status_batch("A-101", "Shortlisted"))
    rt.start(st)
    for _ in range(4):
        rt.step(st)
    assert st.llm_calls == 2 and st.step == 4                  # 2 replies, 4 actions
    policies = [e for e in rt.evidence.events() if e.kind == "policy"]
    assert len(policies) == 4                                   # every action was checked on its own
    assert st.completed == ["shortlist_candidate @ /ats/candidate/A-101 (status=Shortlisted, note=batched)"]
    assert "shortlist_candidate" in rt._ctx(st).completed[0]  # shown to the model as "don't repeat"


def test_rest_of_batch_dropped_after_page_change(make_runtime):
    rt, st = make_runtime([[A(tool="goto", url="/jobs"), A(tool="fill", ref="e6", value="stale ref")]])
    rt.start(st)
    rt.step(st)
    assert st.queue == []                                       # the fill's ref belonged to the old page
    assert not any(e.summary.startswith("fill") for e in rt.evidence.events() if e.kind == "propose")


def test_rest_of_batch_dropped_at_approval(make_runtime):
    rt, st = make_runtime([A(tool="goto", url="/ats/candidate/A-106"),
                           lambda s: [A(tool="select", ref=find_ref(s, r'select "Status"'), value="Rejected"),
                                      A(tool="click", ref=find_ref(s, r'button "Save"')),
                                      A(tool="goto", url="/jobs")]])
    rt.run_until_blocked(rt.start(st))
    assert st.status == RunStatus.AWAITING_APPROVAL and st.queue == []


def test_batched_run_verifies_with_fewer_llm_calls(make_runtime):
    steps = []
    for cid in ["A-101", "A-103", "A-105"]:
        steps += save_status_batch(cid, "Shortlisted")
    for cid in ["A-102", "A-104", "A-106"]:
        steps += save_status_batch(cid, "Not a fit (pending approval)")
    steps += book_batch("S-1", "A-105") + book_batch("S-3", "A-103") + book_batch("S-4", "A-101") + [DONE]
    rt, st = make_runtime(steps)
    rt.run_until_blocked(rt.start(st))
    assert st.status == RunStatus.VERIFIED
    assert st.llm_calls == 19 and st.step == 37                # one-action-per-call would be 37 calls
    assert len(st.completed) == 9


def test_stopped_run_still_reports_the_data(make_runtime):
    rt, st = make_runtime([A(tool="read")] * 5)
    rt.run_until_blocked(rt.start(st))
    assert st.status == RunStatus.STOPPED and "data check: FAIL" in st.stop_reason
    assert any(e.kind == "verify" and "before the agent said done" in e.summary for e in rt.evidence.events())


def test_progress_resets_cycle_guard(make_runtime):
    bounce = [A(tool="goto", url="/ats"), A(tool="goto", url="/jobs")] * 2
    rt, st = make_runtime(bounce + [A(tool="note", value="all resumes noted")] + bounce + [DONE])
    rt.run_until_blocked(rt.start(st))
    assert not any("going in circles" in e.summary for e in rt.evidence.events())


def test_long_note_is_cut_with_feedback(make_runtime):
    rt, st = make_runtime([A(tool="note", value="y" * 1500)])
    rt.start(st)
    rt.step(st)
    assert len(st.notes[0]) == 1000 and "cut to 1000" in st.feedback
