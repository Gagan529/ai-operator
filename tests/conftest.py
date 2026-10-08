"""Shared fixtures: the mock company served in-process, one real headless browser."""
import tempfile
import threading
from pathlib import Path
import time

import pytest
import uvicorn

from aiop.browser import Browser
from aiop.company import Company
from aiop.evidence import EvidenceStore
from aiop.llm import ScriptedProposer
from aiop.runtime import Runtime
from mockapps import loans, recruiting

PORT = 8011
BASE = f"http://127.0.0.1:{PORT}"


V1 = Path(__file__).parent / "fixtures" / "acme_seed_v1.json"
recruiting.SEED = V1                       # tests run on a frozen dataset, not the company's current one
recruiting.INJECTED_ID = "A-106"
TMP_DB = Path(tempfile.mkdtemp(prefix="aiop-tests-"))   # tests never touch the live demo databases in runs/
recruiting.DB = TMP_DB / "acme_talent.db"
loans.DB = TMP_DB / "riverbend_finance.db"


@pytest.fixture(scope="session")
def server():
    recruiting.reset()
    srv = uvicorn.Server(uvicorn.Config(recruiting.app, host="127.0.0.1", port=PORT, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.05)
    yield BASE
    srv.should_exit = True
    t.join(timeout=5)


@pytest.fixture(scope="session")
def browser(server):
    with Browser([server]) as b:
        yield b


@pytest.fixture
def company(server):
    recruiting.reset()
    recruiting.load_faults(flaky=False, silent_fail=False, injection=False)
    c = Company.load("acme_talent", origins=[server])
    c.db_path = recruiting.DB          # the verifier reads the test database too
    return c


@pytest.fixture
def make_runtime(company, browser, tmp_path):
    def make(steps, task=None):
        state = Runtime.new_state(company, task)
        rt = Runtime(company, browser, ScriptedProposer(steps), EvidenceStore(state.run_id, root=tmp_path),
                     sleep=lambda s: None)
        return rt, state
    return make


# ---------- the second company: Riverbend Home Finance (loans), same core ----------
LOANS_PORT = 8012
LOANS = f"http://127.0.0.1:{LOANS_PORT}"


@pytest.fixture(scope="session")
def loans_server():
    loans.reset()
    srv = uvicorn.Server(uvicorn.Config(loans.app, host="127.0.0.1", port=LOANS_PORT, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    while not srv.started:
        time.sleep(0.05)
    yield LOANS
    srv.should_exit = True
    t.join(timeout=5)


@pytest.fixture
def loans_company(loans_server, browser):
    """The session browser, pointed at the lender for this test (one Playwright per thread)."""
    loans.reset()
    loans.load_faults(flaky=False, silent_fail=False, injection=False)
    saved = browser.allowed
    browser.allowed = [loans_server]
    c = Company.load("riverbend_finance", origins=[loans_server])
    c.db_path = loans.DB
    yield c
    browser.allowed = saved


@pytest.fixture
def make_loans_runtime(loans_company, browser, tmp_path):
    def make(steps, task=None):
        state = Runtime.new_state(loans_company, task)
        rt = Runtime(loans_company, browser, ScriptedProposer(steps), EvidenceStore(state.run_id, root=tmp_path),
                     sleep=lambda s: None)
        return rt, state
    return make
