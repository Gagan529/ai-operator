"""The assign tool inside the runtime: allowed by policy, changes no data, answer lands in the notes."""
import json

import pytest
from pydantic import ValidationError

from aiop.assign import assign
from aiop.company import Company
from aiop.llm import _parse_actions
from aiop.models import ProposedAction as A, RunStatus
from aiop.policy import PolicyEngine
from tests.scripts import DONE, screening

PLACE = A(tool="assign", reason="most experienced first",
          items=[{"id": "A-101", "priority": 3, "slots": ["S-1", "S-3", "S-4"]},
                 {"id": "A-105", "priority": 5, "slots": ["S-1", "S-3", "S-4"]},
                 {"id": "A-103", "priority": 4, "slots": ["S-3", "S-4"]}],
          slots=[{"id": "S-1", "order": 1}, {"id": "S-3", "order": 3}, {"id": "S-4", "order": 4}])


def test_assign_result_goes_into_notes_and_evidence(make_runtime):
    rt, st = make_runtime([PLACE] + screening() + [DONE])
    rt.start(st)
    rt.step(st)
    assert st.notes == ["assign result: A-105->S-1, A-103->S-3, A-101->S-4"]
    ev = [e for e in rt.evidence.events() if e.kind == "assign"]
    assert len(ev) == 1 and ev[0].artifact.startswith("assign-")
    saved = json.loads((rt.evidence.dir / ev[0].artifact).read_text(encoding="utf-8"))
    assert saved["result"]["assigned"] == {"A-105": "S-1", "A-103": "S-3", "A-101": "S-4"}
    assert any(e.kind == "policy" and "ALLOW" in e.summary for e in rt.evidence.events())
    rt.run_until_blocked(st)
    assert st.status == RunStatus.VERIFIED


def test_bad_assign_goes_back_to_the_model(make_runtime):
    bad = A(tool="assign", items=[{"id": "X", "priority": 1, "slots": ["S-99"]}], slots=[{"id": "S-1"}])
    rt, st = make_runtime([bad])
    rt.start(st)
    rt.step(st)
    assert st.status == RunStatus.RUNNING and not st.notes
    assert "INVALID_ACTION" in st.feedback and "S-99" in st.feedback


def test_policy_allows_assign_even_with_strict_defaults():
    strict = PolicyEngine({"defaults": {"unknown_effect": "DENY", "unknown_submit": "DENY"}})
    assert strict.decide(PLACE).decision.value == "ALLOW"


def test_model_reply_with_assign_parses_and_extra_keys_are_refused():
    reply = json.dumps({"actions": [{"tool": "assign", "items": [{"id": "a", "priority": 2, "slots": ["t"]}],
                                     "slots": [{"id": "t", "order": 1}], "reason": "r"}]})
    [action] = _parse_actions(reply)
    assert action.items[0].slots == ["t"]
    with pytest.raises(ValidationError):
        _parse_actions(reply.replace('"priority": 2', '"priority": 2, "approved": true'))
    with pytest.raises(ValidationError):
        _parse_actions(json.dumps({"actions": [{"tool": "assign", "items": []}]}))   # slots missing


def test_company_scripted_bookings_are_what_assign_returns():
    """The acme demo books by hand-written steps; they must agree with the solver."""
    script = Company.load("acme_talent").module("scripted")
    got = assign(script.PLACE.items, script.PLACE.slots).assigned
    assert got == {"A-206": "S-11", "A-201": "S-13", "A-203": "S-16", "A-207": "S-17"}
