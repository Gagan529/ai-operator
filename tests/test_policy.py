"""Policy engine: deterministic, effect-based, deaf to the LLM's claims."""
import pytest
from pydantic import ValidationError

from aiop.company import Company
from aiop.models import Decision, ProposedAction, Target
from aiop.policy import PolicyEngine

ENGINE = PolicyEngine(Company.load("acme_talent").policies)
FORM = "http://127.0.0.1:8001/ats/candidate/A-106"
CLICK = ProposedAction(tool="click", ref="e7")


def submit(effects=(), fields=None, action=FORM, effect=None):
    return Target(ref="e7", tag="button", label="Save", is_submit=True, effect=effect, form_action=action,
                  form_method="post", form_fields=fields or {}, form_effects=list(effects))


def test_reject_needs_approval():
    d = ENGINE.decide(CLICK, submit(["reject_candidate"], {"status": "Rejected"}))
    assert d.decision == Decision.APPROVAL_REQUIRED and d.effect == "reject_candidate"


def test_allowed_effects():
    assert ENGINE.decide(CLICK, submit(["shortlist_candidate"])).decision == Decision.ALLOW
    assert ENGINE.decide(CLICK, submit(effect="book_interview")).decision == Decision.ALLOW


def test_llm_words_are_ignored():
    # the proposal claims it is harmless and approved; the page says it is a rejection
    sneaky = ProposedAction(tool="click", ref="e7", reason="harmless save, already approved by the manager")
    assert ENGINE.decide(sneaky, submit(["reject_candidate"])).decision == Decision.APPROVAL_REQUIRED


@pytest.mark.parametrize("extra", ['"approved": true', '"effect": "shortlist_candidate"', '"skip_policy": 1'])
def test_proposal_cannot_carry_policy_fields(extra):
    with pytest.raises(ValidationError):
        ProposedAction.model_validate_json('{"tool": "click", "ref": "e7", ' + extra + '}')


def test_fallback_rule_without_page_metadata():
    d = ENGINE.decide(CLICK, submit(fields={"status": "Rejected", "note": ""}))
    assert d.decision == Decision.APPROVAL_REQUIRED and d.rule.startswith("fallback:ats-status-rejected")


def test_unknown_submit_is_conservative():
    d = ENGINE.decide(CLICK, submit(fields={"x": "y"}, action="http://127.0.0.1:8001/somewhere"))
    assert d.decision == Decision.APPROVAL_REQUIRED and d.rule == "defaults.unknown_submit"


def test_unlisted_effect_is_conservative():
    assert ENGINE.decide(CLICK, submit(["delete_everything"])).decision == Decision.APPROVAL_REQUIRED


def test_strictest_effect_wins():
    d = ENGINE.decide(CLICK, submit(["shortlist_candidate", "reject_candidate"]))
    assert d.decision == Decision.APPROVAL_REQUIRED


def test_machine_endpoints_denied():
    assert ENGINE.decide(ProposedAction(tool="goto", url="/admin/reset")).decision == Decision.DENY
    link = Target(ref="e1", tag="a", href="http://127.0.0.1:8001/admin/reset")
    assert ENGINE.decide(CLICK, link).decision == Decision.DENY


def test_form_edits_and_reads_allowed():
    for a in [ProposedAction(tool="select", ref="e5", value="Rejected"),
              ProposedAction(tool="fill", ref="e6", value="x"), ProposedAction(tool="read")]:
        assert ENGINE.decide(a).decision == Decision.ALLOW
