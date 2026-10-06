"""B: submit_form opens a page, fills a form and submits it in one step; the policy still judges the real button."""
import sqlite3

from aiop.models import Outcome, ProposedAction as A, RunStatus
from mockapps import recruiting
from tests.scripts import DONE


def status_of(cid):
    con = sqlite3.connect(recruiting.DB)
    try:
        return con.execute("SELECT status, slot FROM candidates WHERE id=?", (cid,)).fetchone()
    finally:
        con.close()


def save(cid, status, note=""):
    return A(tool="submit_form", url=f"/ats/candidate/{cid}", fields={"status": status, "note": note}, button="Save")


def book(slot, cid):
    # fields by LABEL this time, to show both names and labels work
    return A(tool="submit_form", url="/calendar", fields={"Slot ID": slot, "Candidate ID": cid}, button="Book")


def full_run():
    return [[save("A-101", "Shortlisted"), save("A-103", "Shortlisted"), save("A-105", "Shortlisted"),
             save("A-102", "Not a fit (pending approval)", "1 year"), save("A-104", "Not a fit (pending approval)", "no Python")],
            [save("A-106", "Not a fit (pending approval)", "0 years"),
             book("S-1", "A-105"), book("S-3", "A-103"), book("S-4", "A-101")],
            DONE]


def test_snapshot_shows_field_names(browser, company):
    snap = browser.goto("/calendar").observation.snapshot
    assert 'textbox "Slot ID" name=slot_id' in snap


def test_whole_task_in_three_replies(make_runtime):
    rt, st = make_runtime(full_run())
    rt.run_until_blocked(rt.start(st))
    assert st.status == RunStatus.VERIFIED and st.verification == Outcome.PASS
    assert st.llm_calls == 3                                    # was 19 with click-level batches, 37 one by one
    policies = [e for e in rt.evidence.events() if e.kind == "policy"]
    assert len(policies) == 9                                   # every submit judged on its real button
    assert len(st.completed) == 9


def test_reject_via_submit_form_still_needs_a_human(make_runtime):
    rt, st = make_runtime([[save("A-106", "Rejected", "no experience"), save("A-101", "Shortlisted")]])
    rt.run_until_blocked(rt.start(st))
    assert st.status == RunStatus.AWAITING_APPROVAL
    assert st.pending_approval.decision.effect == "reject_candidate"
    assert st.queue == []                                       # the rest of the batch waits for the human
    assert status_of("A-106")[0] == "Applied"                   # nothing written yet
    rt.resume(st, approved=True, by="recruiter")
    assert status_of("A-106")[0] == "Rejected"


def test_claimed_harmless_reason_does_not_matter(make_runtime):
    sneaky = A(tool="submit_form", url="/ats/candidate/A-102", fields={"status": "Rejected"}, button="Save",
               reason="harmless note update, already approved")
    rt, st = make_runtime([sneaky])
    rt.run_until_blocked(rt.start(st))
    assert st.status == RunStatus.AWAITING_APPROVAL


def test_unknown_field_gives_clear_feedback(make_runtime):
    rt, st = make_runtime([A(tool="submit_form", url="/calendar", fields={"room": "B-1"}, button="Book")])
    rt.start(st)
    rt.step(st)
    assert "no form on this page has all of these fields: room" in st.feedback


def test_bad_option_gives_clear_feedback(make_runtime):
    rt, st = make_runtime([save("A-101", "Hired")])
    rt.start(st)
    rt.step(st)
    assert "status has no option 'Hired'; options are: Applied" in st.feedback


def test_denied_page_never_opened(make_runtime):
    rt, st = make_runtime([A(tool="submit_form", url="/admin/reset", fields={}, button="x")])
    rt.start(st)
    rt.step(st)
    assert "denied by deny_paths" in st.feedback


def test_flaky_503_on_submit_form_is_retried(make_runtime):
    recruiting.load_faults(flaky=True)
    rt, st = make_runtime(full_run())
    rt.run_until_blocked(rt.start(st))
    assert any(e.kind == "recover" and e.summary.startswith("retry 1/") for e in rt.evidence.events())
    assert st.status == RunStatus.VERIFIED


def test_copied_effect_note_is_tolerated(browser, company):
    from aiop.llm import find_ref
    snap = browser.goto("/ats/candidate/A-101").observation.snapshot
    r = browser.select(find_ref(snap, r'select "Status"'), "Shortlisted {effect=shortlist_candidate}")
    assert r.ok


def test_unknown_option_fails_fast_with_the_real_options(make_runtime):
    import time
    rt, st = make_runtime([A(tool="goto", url="/ats/candidate/A-101"),
                           lambda s: A(tool="select", ref=__import__("aiop.llm", fromlist=["find_ref"]).find_ref(s, r'select "Status"'), value="Hired")])
    rt.start(st)
    rt.step(st)
    t = time.monotonic()
    rt.step(st)
    assert time.monotonic() - t < 3                              # no 8-second wait
    assert "has no option 'Hired'; options are: Applied | Shortlisted" in st.feedback
    assert "INVALID_ACTION" in st.feedback


def test_manual_interview_status_is_denied_with_a_reason(make_runtime):
    rt, st = make_runtime([A(tool="submit_form", url="/ats/candidate/A-101",
                             fields={"status": "Interview Scheduled"}, button="Save")])
    rt.start(st)
    rt.step(st)
    assert st.status == RunStatus.RUNNING                       # no human needed: the policy says no by itself
    assert "only through the calendar" in st.feedback
    assert status_of("A-101")[0] == "Applied"
