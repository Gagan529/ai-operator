"""Verifier: reads the app's database, never the page or the LLM."""
import sqlite3

from aiop.models import Outcome
from aiop.verify import Verifier
from mockapps import recruiting


def correct_db(reject: str | None = None):
    recruiting.reset()
    con = sqlite3.connect(recruiting.DB)
    for cid, slot in [("A-105", "S-1"), ("A-103", "S-3"), ("A-101", "S-4")]:
        con.execute("UPDATE candidates SET status='Interview Scheduled', slot=? WHERE id=?", (slot, cid))
        con.execute("UPDATE slots SET status='booked', candidate=? WHERE id=?", (cid, slot))
    for cid in ["A-102", "A-104", "A-106"]:
        con.execute("UPDATE candidates SET status=? WHERE id=?",
                    ("Rejected" if cid == reject else "Not a fit (pending approval)", cid))
    con.commit()
    return con


class verifier:
    """The verifier as the runtime uses it for the company's default task."""
    def __init__(self, company, task=None):
        self.v = Verifier(company.db_path, company.invariants)
        self.goals = company.goal_checks(task or company.default_task)

    def run(self, facts=None):
        return self.v.run(facts, self.goals)


def test_correct_db_passes(company):
    correct_db().close()
    assert verifier(company).run().outcome == Outcome.PASS


def test_untouched_db_fails(company):
    r = verifier(company).run()
    assert r.outcome == Outcome.FAIL
    assert not next(c for c in r.checks if c.name == "every applicant screened").passed


def test_rejection_without_approval_fails(company):
    correct_db(reject="A-106").close()
    assert verifier(company).run({"approved": {}}).outcome == Outcome.FAIL
    assert verifier(company).run({"approved": {"reject_candidate": 1}}).outcome == Outcome.PASS


def test_wrong_order_is_partial(company):
    con = correct_db()
    con.execute("UPDATE candidates SET slot='S-4' WHERE id='A-105'")
    con.execute("UPDATE candidates SET slot='S-1' WHERE id='A-101'")
    con.execute("UPDATE slots SET candidate='A-101' WHERE id='S-1'")
    con.execute("UPDATE slots SET candidate='A-105' WHERE id='S-4'")
    con.commit()
    con.close()
    r = verifier(company).run()
    assert r.outcome == Outcome.PARTIAL
    assert [c.name for c in r.checks if not c.passed] == ["more experienced candidates interviewed first (POLICY-3)"]


# ---------- invariants vs goal checks ----------
OTHER_TASK = "Move the most experienced candidate to the latest free slot"


def test_task_without_goal_checks_is_unverified_not_pass(company):
    correct_db().close()
    r = verifier(company, OTHER_TASK).run()
    assert r.outcome == Outcome.UNVERIFIED
    assert {c.kind for c in r.checks} == {"invariant"} and all(c.passed for c in r.checks)


def test_broken_invariant_fails_any_task(company):
    correct_db(reject="A-106").close()                      # a rejection nobody approved
    assert verifier(company, OTHER_TASK).run({"approved": {}}).outcome == Outcome.FAIL


def test_untouched_db_is_unverified_for_an_unknown_task(company):
    """Nothing was broken, and nobody checked the goal: neither PASS nor FAIL."""
    assert verifier(company, OTHER_TASK).run().outcome == Outcome.UNVERIFIED


def test_default_task_matched_ignoring_case_and_spacing(company):
    task = "  " + company.default_task.upper().replace(" ", "   ") + "\t"
    assert company.goal_checks(task) == company.goal_checks(company.default_task) is not None
    kinds = [c.kind for c in verifier(company).run().checks]
    assert kinds.count("invariant") == len(company.invariants) and "goal" in kinds
