"""The live proposer's plumbing, with a fake client (no key, no network)."""
from types import SimpleNamespace

import httpx
import openai
import pytest

from aiop.llm import Context, OpenAICompatibleProposer, ProposalError
from aiop.models import FailureKind, RunStatus

CTX = Context(task="t", sop="s", snapshot="URL: x")


class FakeCompletions:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=r))],
                               usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10,
                                                     prompt_tokens_details=SimpleNamespace(cached_tokens=40),
                                                     completion_tokens_details=SimpleNamespace(reasoning_tokens=7)))


def proposer(replies, monkeypatch, **env):
    for k, v in {"LLM_REASONING_EFFORT": "low", "LLM_MIN_INTERVAL_S": "0", **env}.items():
        monkeypatch.setenv(k, v)
    fake = FakeCompletions(replies)
    p = OpenAICompatibleProposer(model="m", client=SimpleNamespace(chat=SimpleNamespace(completions=fake)))
    return p, fake


def rate_limited():
    req = httpx.Request("POST", "https://x")
    return openai.RateLimitError("429", response=httpx.Response(429, request=req), body=None)


def test_valid_reply_and_settings_sent(monkeypatch):
    p, fake = proposer(['```json\n{"tool": "click", "ref": "e7", "reason": "save"}\n```'], monkeypatch)
    a = p.propose_actions(CTX)[0]
    assert a.tool == "click" and a.ref == "e7"
    assert fake.calls[0]["reasoning_effort"] == "low"
    assert fake.calls[0]["response_format"] == {"type": "json_object"}
    assert (p.tokens_in, p.tokens_out) == (100, 10)


def test_reasoning_effort_can_be_omitted(monkeypatch):
    p, fake = proposer(['{"tool": "read"}'], monkeypatch, LLM_REASONING_EFFORT="")
    p.propose_actions(CTX)[0]
    assert "reasoning_effort" not in fake.calls[0]


def test_invalid_reply_gets_one_repair(monkeypatch):
    p, fake = proposer(['{"tool": "click", "ref": "e7", "approved": true}', '{"tool": "click", "ref": "e7"}'],
                       monkeypatch)
    assert p.propose_actions(CTX)[0].ref == "e7"
    assert "Invalid reply" in fake.calls[1]["messages"][-1]["content"]


def test_two_bad_replies_give_up(monkeypatch):
    p, _ = proposer(["not json", '{"tool": "teleport"}'], monkeypatch)
    with pytest.raises(ProposalError) as e:
        p.propose_actions(CTX)[0]
    assert not e.value.transient


def test_rate_limit_is_transient(monkeypatch):
    p, _ = proposer([rate_limited()], monkeypatch)
    with pytest.raises(ProposalError) as e:
        p.propose_actions(CTX)[0]
    assert e.value.transient


def test_runtime_treats_provider_outage_as_transient(make_runtime):
    class Down:
        tokens_in = tokens_out = 0

        def propose_plan(self, ctx):
            raise ProposalError("429", transient=True)

        def propose_actions(self, ctx):
            raise ProposalError("429", transient=True)

    rt, st = make_runtime([])
    rt.proposer = Down()
    rt.run_until_blocked(rt.start(st))
    failures = [e for e in rt.evidence.events() if e.kind == "failure"]
    assert failures and all(e.summary.startswith(FailureKind.TRANSIENT.value) for e in failures)
    assert st.status == RunStatus.STOPPED          # bounded: it gives up instead of looping forever


def test_batch_reply_parsed(monkeypatch):
    p, _ = proposer(['{"actions": [{"tool": "fill", "ref": "e5", "value": "S-1"}, '
                     '{"tool": "fill", "ref": "e6", "value": "A-105"}, {"tool": "click", "ref": "e7"}]}'], monkeypatch)
    assert [a.tool for a in p.propose_actions(CTX)] == ["fill", "fill", "click"]


def test_done_must_be_alone(monkeypatch):
    p, fake = proposer(['{"actions": [{"tool": "click", "ref": "e7"}, {"tool": "done"}]}',
                        '{"actions": [{"tool": "done", "reason": "finished"}]}'], monkeypatch)
    assert [a.tool for a in p.propose_actions(CTX)] == ["done"]
    assert "done must be the only action" in fake.calls[1]["messages"][-1]["content"]


def test_batch_too_long_rejected(monkeypatch):
    six = '{"actions": [' + ",".join(['{"tool": "read"}'] * 6) + "]}"
    p, _ = proposer([six, six], monkeypatch)
    with pytest.raises(ProposalError):
        p.propose_actions(CTX)


def test_usage_counts_cache_and_reasoning(monkeypatch):
    p, _ = proposer(['{"tool": "read"}'], monkeypatch)
    p.propose_actions(CTX)
    assert (p.tokens_in, p.tokens_cached, p.tokens_out, p.tokens_reasoning) == (100, 40, 10, 7)
    assert p.last_usage["attempts"] == 1 and p.last_usage["cached"] == 40 and "ms" in p.last_usage


def test_usage_recorded_even_when_the_reply_is_unusable(monkeypatch):
    p, _ = proposer(["nope", "still nope"], monkeypatch)
    with pytest.raises(ProposalError):
        p.propose_actions(CTX)
    assert p.last_usage["attempts"] == 2 and p.last_usage["in"] == 200      # failed calls still cost tokens
