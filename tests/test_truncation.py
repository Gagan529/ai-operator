"""A reply cut off at the output limit (finish_reason="length") is never half-used: it is logged,
and the model is told why it failed and to send a shorter reply."""
import json
from types import SimpleNamespace as N

import pytest

from aiop.llm import TRUNCATED, Context, OpenAICompatibleProposer, ProposalError

GOOD = json.dumps({"actions": [{"tool": "goto", "url": "/a", "reason": "r"}]})
CUT = '{"actions":[{"tool":"goto","url":"/a","reason":"r"},{"tool":"submit_form","url":"/pay/R-1","fiel'


class FakeAPI:
    """Returns the given (text, finish_reason) replies in order and keeps every request."""
    def __init__(self, *replies):
        self.chat = self.completions = self
        self.replies = list(replies)
        self.requests = []

    def create(self, **kw):
        self.requests.append(kw["messages"])
        text, finish = self.replies.pop(0)
        return N(choices=[N(message=N(content=text), finish_reason=finish)], usage=None)


def ask(api):
    p = OpenAICompatibleProposer(model="m", api_key="k", client=api)
    return p, p.propose_actions(Context(task="t", sop="s", snapshot="page"))


def test_cut_off_reply_gets_a_specific_repair_and_is_logged():
    api = FakeAPI((CUT, "length"), (GOOD, "stop"))
    p, actions = ask(api)
    assert [a.tool for a in actions] == ["goto"]           # the repaired reply, nothing from the cut one
    assert api.requests[1][-1]["content"] == TRUNCATED     # told it was cut off, and to send fewer actions
    assert p.last_usage["truncated"] == 1 and p.last_usage["attempts"] == 2


def test_cut_off_twice_gives_a_clear_error():
    with pytest.raises(ProposalError, match="cut off at the output limit") as e:
        ask(FakeAPI((CUT, "length"), (CUT, "length")))
    assert not e.value.transient                           # the runtime goes back to the model, then budgets stop it


def test_ordinary_bad_reply_keeps_the_generic_repair():
    api = FakeAPI(('{"actions": []}', "stop"), (GOOD, "stop"))
    p, _ = ask(api)
    assert api.requests[1][-1]["content"].startswith("Invalid reply:")
    assert p.last_usage["truncated"] == 0
