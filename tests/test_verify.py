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


def verifier(company):
    return Verifier(company.db_path, company.checks)


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
