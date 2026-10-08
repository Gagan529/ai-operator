"""Hostile input: SQL injection into the apps, harmful verifier SQL, leaving the allowed site, bad console requests."""
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

import pytest

from aiop.models import FailureKind, ProposedAction as A
from aiop.verify import Verifier
from mockapps import loans, recruiting
from tests.scripts import DONE

PAYLOADS = ["x'); DROP TABLE candidates; --", "' OR '1'='1", "1; DELETE FROM slots", "Robert'); --\"",
            "%' UNION SELECT * FROM ground_truth --"]


def rows(db, sql):
    con = sqlite3.connect(db)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def post(base, path, **fields):
    data = urllib.parse.urlencode(fields).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(base + path, data=data, method="POST"), timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def get(base, path):
    try:
        with urllib.request.urlopen(base + urllib.parse.quote(path), timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


# ---------- SQL injection straight at the apps ----------
def test_recruiting_app_stores_payloads_as_plain_text(company, server):
    before = rows(recruiting.DB, "SELECT id, status FROM candidates ORDER BY id")
    for p in PAYLOADS:
        post(server, "/ats/candidate/A-101", status="Shortlisted", note=p)
        assert rows(recruiting.DB, "SELECT note FROM candidates WHERE id='A-101'") == [(p,)]   # stored as text
        post(server, "/calendar/book", slot_id=p, candidate_id="A-102")
        post(server, "/calendar/book", slot_id="S-1", candidate_id=p)
        assert get(server, f"/ats/candidate/{p}")[1].count("No such candidate") == 1
    after = rows(recruiting.DB, "SELECT id, status FROM candidates ORDER BY id")
    assert [r[0] for r in after] == [r[0] for r in before]                 # no table dropped, no row lost
    assert dict(after)["A-102"] == dict(before)["A-102"]                    # nobody else changed
    assert rows(recruiting.DB, "SELECT count(*) FROM slots WHERE status='booked'") == [(0,)]


def test_loans_app_stores_payloads_as_plain_text(loans_company, loans_server):
    for p in PAYLOADS:
        post(loans_server, "/los/application/L-303", decision="Referred to credit committee", note=p)
        assert rows(loans.DB, "SELECT note FROM applications WHERE id='L-303'") == [(p,)]
        status, page = post(loans_server, "/disburse/L-301", amount=p)
        assert "Disbursement failed" in page
        assert get(loans_server, f"/los/application/{p}")[1].count("No such application") == 1
    assert rows(loans.DB, "SELECT count(*) FROM disbursements") == [(0,)]
    assert rows(loans.DB, "SELECT count(*) FROM applications") == [(8,)]
    assert rows(loans.DB, "SELECT count(*) FROM applications WHERE decision != 'Pending'") == [(1,)]   # only L-303


# ---------- the same payloads proposed by a (fooled) model, through the whole runtime ----------
def test_payload_in_a_proposed_form_is_just_text(make_runtime):
    p = PAYLOADS[0]
    rt, st = make_runtime([A(tool="submit_form", url="/ats/candidate/A-101",
                             fields={"status": "Shortlisted", "note": p}, button="Save"),
                           A(tool="submit_form", url="/calendar", fields={"slot_id": PAYLOADS[1], "candidate_id": "A-101"},
                             button="Book"), DONE])
    rt.run_until_blocked(rt.start(st))
    assert rows(recruiting.DB, "SELECT note FROM candidates WHERE id='A-101'") == [(p,)]
    assert rows(recruiting.DB, "SELECT count(*) FROM candidates") == [(6,)]
    assert rows(recruiting.DB, "SELECT count(*) FROM slots WHERE status='booked'") == [(0,)]
    assert any(e.kind == "failure" and "APP_ERROR" in e.summary for e in rt.evidence.events())   # booking refused
    assert st.verification is not None                                     # the verifier still ran normally


# ---------- harmful SQL in the verifier itself ----------
@pytest.mark.parametrize("sql", ["DROP TABLE candidates", "DELETE FROM candidates",
                                 "UPDATE candidates SET status='Rejected'", "INSERT INTO slots VALUES ('X','x','x','free',NULL)"])
def test_verifier_cannot_change_the_database(company, sql):
    before = rows(recruiting.DB, "SELECT * FROM candidates ORDER BY id")
    r = Verifier(company.db_path, [{"name": "hostile", "sql": sql, "expect": 0}]).run({}, [])
    check = r.checks[0]
    assert not check.passed and "query error" in str(check.actual)          # read-only connection refuses it
    assert rows(recruiting.DB, "SELECT * FROM candidates ORDER BY id") == before


# ---------- leaving the company's systems ----------
@pytest.mark.parametrize("url", ["https://example.com/", "http://127.0.0.1:9/steal", "file:///C:/Windows/win.ini"])
def test_off_site_navigation_is_refused(make_runtime, url):
    rt, st = make_runtime([A(tool="goto", url=url), DONE])
    rt.run_until_blocked(rt.start(st))
    fails = [e for e in rt.evidence.events() if e.kind == "failure"]
    assert fails and fails[0].summary.startswith(FailureKind.POLICY_VIOLATION.value)
    assert rt.browser.is_allowed(rt.browser.page.url)                     # still on the company's app


# ---------- bad requests to the operator console ----------
@pytest.fixture(scope="module")
def console():
    from fastapi.testclient import TestClient
    from console import app as console_app
    return TestClient(console_app.app, raise_server_exceptions=False)


@pytest.mark.parametrize("company", ["../../etc", "..\\..\\Windows", "acme_talent/../../x", "no_such_company", ""])
def test_console_refuses_unknown_or_traversing_company(console, company):
    r = console.post("/api/runs", json={"company": company, "proposer": "scripted"})
    assert r.status_code in (400, 404, 422)


def test_console_rejects_bad_requests(console):
    assert console.post("/api/runs", json={"company": "acme_talent", "proposer": "root"}).status_code == 422
    assert console.get("/api/runs/does-not-exist").status_code == 404
    assert console.post("/api/runs/does-not-exist/answer", json={"approved": True}).status_code == 409
    assert console.get("/runs/../.env").status_code == 404                 # static files stay inside runs/
