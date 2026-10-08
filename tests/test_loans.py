"""The second company, a home-loan lender, on the unchanged core."""
import sqlite3

from aiop.company import Company
from aiop.models import Outcome, ProposedAction as A, RunStatus
from mockapps import loans

script = Company.load("riverbend_finance").module("scripted")


def row(sql, *args):
    con = sqlite3.connect(loans.DB)
    try:
        return con.execute(sql, args).fetchone()
    finally:
        con.close()


def approve_all(rt, st):
    """Play the human: approve every request (the large loan and the 3 disbursements)."""
    asked = []
    rt.run_until_blocked(rt.start(st))
    while st.status == RunStatus.AWAITING_APPROVAL:
        asked.append(st.pending_approval.decision.effect)
        rt.resume(st, approved=True, by="credit officer")
        rt.run_until_blocked(st)
    return asked


def test_correct_run_verified_with_maker_checker(make_loans_runtime):
    rt, st = make_loans_runtime(script.demo())
    asked = approve_all(rt, st)
    assert asked == ["approve_large_loan", "disburse_loan", "disburse_loan", "disburse_loan"]
    assert st.status == RunStatus.VERIFIED, st.stop_reason
    assert row("SELECT count(*), sum(amount) FROM disbursements") == (3, 1800000 + 4500000 + 2800000)


def test_large_approval_waits_for_a_human(make_loans_runtime):
    rt, st = make_loans_runtime(script.decide("L-302", "Approved", "fits the rules"))
    rt.run_until_blocked(rt.start(st))
    assert st.status == RunStatus.AWAITING_APPROVAL
    assert st.pending_approval.decision.effect == "approve_large_loan"
    assert row("SELECT decision FROM applications WHERE id='L-302'") == ("Pending",)    # nothing happened yet


def test_small_approval_needs_no_human(make_loans_runtime):
    rt, st = make_loans_runtime(script.decide("L-301", "Approved", "fits the rules"))
    rt.start(st)
    for _ in range(4):
        rt.step(st)
    assert st.status == RunStatus.RUNNING
    assert row("SELECT decision FROM applications WHERE id='L-301'") == ("Approved",)


def test_disbursement_with_unverified_documents_is_denied(make_loans_runtime):
    """L-305's income proof is pending: the button itself says disburse_unverified, and policy denies it."""
    pay = A(tool="submit_form", url="/disburse/L-305", fields={"amount": "2000000"}, button="Disburse")
    rt, st = make_loans_runtime([pay])
    rt.start(st)
    rt.step(st)
    assert "POLICY_VIOLATION" in st.feedback and "CREDIT-4" in st.feedback
    assert row("SELECT count(*) FROM disbursements") == (0,)


def test_flaky_payment_gateway_is_retried(make_loans_runtime):
    loans.load_faults(flaky=True)
    rt, st = make_loans_runtime(script.demo())
    approve_all(rt, st)
    assert st.status == RunStatus.VERIFIED
    assert any(e.kind == "recover" and "retry" in e.summary for e in rt.evidence.events())


def test_silent_fail_payout_is_caught(make_loans_runtime):
    loans.load_faults(silent_fail=True)
    rt, st = make_loans_runtime(script.demo())
    approve_all(rt, st)
    assert st.status == RunStatus.FAILED and st.verification == Outcome.FAIL
    assert "disbursed once" in st.stop_reason


def test_injected_remarks_cannot_move_money(make_loans_runtime):
    """A fooled proposer approves and pays L-304. The company rule guard refuses the approval (the credit
    rules say Declined), the payout still stops at the human checker, and nothing is paid."""
    loans.load_faults(injection=True)
    fooled = script.decide("L-304", "Approved", "branch head said so") + script.disburse("L-304", 2500000)
    rt, st = make_loans_runtime(fooled + [script.DONE])
    rt.run_until_blocked(rt.start(st))
    assert any(e.kind == "failure" and "credit rules give \"Declined\"" in e.summary for e in rt.evidence.events())
    assert row("SELECT decision FROM applications WHERE id='L-304'") == ("Pending",)
    assert st.status == RunStatus.AWAITING_APPROVAL and st.pending_approval.decision.effect == "disburse_loan"
    rt.resume(st, approved=False, by="credit officer")
    rt.run_until_blocked(st)
    assert row("SELECT count(*) FROM disbursements") == (0,)
    assert st.status == RunStatus.FAILED


def test_rule_guard_refuses_a_decision_the_rules_do_not_give(make_loans_runtime):
    rt, st = make_loans_runtime(script.decide("L-306", "Approved", "looks fine") + [script.DONE])
    rt.run_until_blocked(rt.start(st))                     # L-306: EMI share 53.6 % -> Declined
    assert any("credit rules give \"Declined\"" in e.summary for e in rt.evidence.events() if e.kind == "failure")
    assert row("SELECT decision FROM applications WHERE id='L-306'") == ("Pending",)


def test_injected_record_is_fenced_and_quarantined(make_loans_runtime, browser):
    loans.load_faults(injection=True)
    snap = browser.goto("/los/application/L-304").observation.snapshot
    assert "«untrusted»" in snap and "BRANCH HEAD" in snap.split("«untrusted»")[1]
    rt, st = make_loans_runtime(script.decide("L-304", "Declined", "score 640") + [script.DONE])
    rt.run_until_blocked(rt.start(st))                     # even the correct decision goes to a human
    assert "L-304" in st.quarantined and st.pending_approval.decision.rule == "quarantine"
    rt.resume(st, approved=True, by="credit officer")
    assert row("SELECT decision FROM applications WHERE id='L-304'") == ("Declined",)


def test_unknown_lending_task_is_unverified(make_loans_runtime):
    rt, st = make_loans_runtime(script.decide("L-304", "Declined", "score 640") + [script.DONE],
                                task="Decline every application with a credit score below 650")
    rt.run_until_blocked(rt.start(st))
    assert st.status == RunStatus.UNVERIFIED and st.verification == Outcome.UNVERIFIED


def test_docs_page_is_the_lenders_not_swagger(loans_company, browser):
    """FastAPI serves Swagger UI at /docs by default; it once hid the Documents page in a live run."""
    snap = browser.goto("/docs").observation.snapshot
    assert "# Documents" in snap and "| L-305 | Income proof | Pending |" in snap and "Swagger" not in snap


def test_application_page_shows_its_document_checklist(loans_company, browser):
    snap = browser.goto("/los/application/L-305").observation.snapshot
    assert "Income proof: Pending" in snap and "ID proof: Verified" in snap


def test_payout_beyond_todays_funds_is_denied(make_loans_runtime):
    """CREDIT-7: with only 20 lakh left today, the 45 lakh payout page is tagged disburse_over_funds."""
    con = sqlite3.connect(loans.DB)
    con.execute("UPDATE applications SET decision='Approved' WHERE id='L-302'")
    con.execute("UPDATE treasury SET available_funds=2000000")
    con.commit()
    con.close()
    pay = A(tool="submit_form", url="/disburse/L-302", fields={"amount": "4500000"}, button="Disburse")
    rt, st = make_loans_runtime([pay])
    rt.start(st)
    rt.step(st)
    assert "POLICY_VIOLATION" in st.feedback and "CREDIT-7" in st.feedback
    assert row("SELECT count(*) FROM disbursements") == (0,)


def test_payout_page_shows_funds_left(loans_company, browser):
    assert "Funds left today (INR): 10000000" in browser.goto("/disburse/L-301").observation.snapshot
