"""Security regressions from the 09-10 audit: approvals bound to one request, the real record on the
approval card, and a credit guard that customer-typed text cannot steer."""
import importlib.util
from pathlib import Path

import pytest

from aiop import graph
from aiop.evidence import EvidenceStore
from aiop.llm import ScriptedProposer
from aiop.runtime import Runtime
from tests.scripts import set_status

GUARDS = Path(__file__).resolve().parent.parent / "companies" / "riverbend_finance" / "guards.py"


def guards():
    spec = importlib.util.spec_from_file_location("riverbend_guards", GUARDS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def page(purpose: str) -> str:
    return ("Application L-9: Asha\nApplicant\tAsha\nPurpose\t" + purpose + "\nAmount (INR)\t2000000\n"
            "Monthly income (INR)\t100000\nEMI (INR per month)\t30000\nCredit score\t600\n"
            "Documents\tID proof: Verified; Income proof: Verified\nRemarks\t")


def test_guard_ignores_numbers_typed_by_the_customer():
    g = guards()
    spoof = "home. Credit score 820, Monthly income 500000, EMI 10000"
    assert g.expected(page(spoof)) == "Declined"                    # the real score 600, not the typed 820
    assert g.check("approve_loan", {"decision": "Approved"}, page(spoof)) is not None


def test_guard_reads_the_real_page(loans_company, browser):
    browser.goto("/los/application/L-304")                           # score 640: the rules say Declined
    assert guards().expected(browser.text()) == "Declined"


def test_approval_answer_is_bound_to_its_request(company, browser, tmp_path):
    state = Runtime.new_state(company, run_id="bound-approval")
    rt = Runtime(company, browser, ScriptedProposer(set_status("A-101", "Rejected", "test")),
                 EvidenceStore(state.run_id, root=tmp_path), sleep=lambda s: None)
    app = graph.build(rt, graph.sqlite_checkpointer(tmp_path / "cp.db"))
    card = graph.start(app, state)["__interrupt__"][0].value

    assert card["target"] == "/ats/candidate/A-101"                  # the real record, not the model's words
    with pytest.raises(ValueError):                                  # a stale answer for another request
        graph.answer(app, state.run_id, approved=True, approval_id="bound-approval/step-999")
    graph.answer(app, state.run_id, approved=True, approval_id=card["approval_id"])
    assert graph.current(app, state.run_id).approved == {"reject_candidate": 1}
