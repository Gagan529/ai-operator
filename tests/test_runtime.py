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
