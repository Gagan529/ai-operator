"""Shared fixtures: the mock company served in-process, one real headless browser."""
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
from mockapps import recruiting

PORT = 8011
BASE = f"http://127.0.0.1:{PORT}"


V1 = Path(__file__).parent / "fixtures" / "acme_seed_v1.json"
recruiting.SEED = V1                       # tests run on a frozen dataset, not the company's current one
recruiting.INJECTED_ID = "A-106"


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
    return Company.load("acme_talent", origins=[server])


@pytest.fixture
def make_runtime(company, browser, tmp_path):
    def make(steps, task=None):
        state = Runtime.new_state(company, task)
        rt = Runtime(company, browser, ScriptedProposer(steps), EvidenceStore(state.run_id, root=tmp_path),
                     sleep=lambda s: None)
        return rt, state
    return make
