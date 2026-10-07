"""A human's denial is final: remembered, shown every step, and refused if proposed again.
Found in a live loans run, where the model asked four times to pay out a loan the human had denied."""
from aiop.llm import Context, OpenAICompatibleProposer
from aiop.models import ProposedAction as A, RunStatus
from tests.scripts import DONE, screening, set_status


def reject(cid):
    return A(tool="submit_form", url=f"/ats/candidate/{cid}", fields={"status": "Rejected"}, button="Save")


def test_denied_change_is_remembered_and_never_asked_again(make_runtime):
    rt, st = make_runtime(screening() + [reject("A-106"), reject("A-106"), DONE])
    rt.run_until_blocked(rt.start(st))
    assert st.status == RunStatus.AWAITING_APPROVAL
    rt.resume(st, approved=False, by="recruiter")
    assert st.denied and st.denied[0].startswith("reject_candidate @ /ats/candidate/A-106")
    rt.step(st)                                            # the same rejection, proposed again
    assert st.status == RunStatus.RUNNING                  # no second approval card for the human
    assert "already DENIED exactly this change" in st.feedback
    asks = [e for e in rt.evidence.events() if e.kind == "approval_request"]
    assert len(asks) == 1
    rt.run_until_blocked(st)
    assert st.status == RunStatus.VERIFIED                 # the rest of the work still verifies


def test_a_different_change_is_still_asked(make_runtime):
    """Only the exact denied change is refused: rejecting another candidate still goes to the human."""
    rt, st = make_runtime(screening() + [reject("A-106"), reject("A-104")])
    rt.run_until_blocked(rt.start(st))
    rt.resume(st, approved=False, by="recruiter")
    rt.run_until_blocked(st)
    assert st.status == RunStatus.AWAITING_APPROVAL
    assert st.pending_approval.target.form_action.endswith("/ats/candidate/A-104")


def test_denied_list_is_in_every_prompt():
    seen = {}

    class Fake:
        def __init__(self):
            self.chat = self
            self.completions = self

        def create(self, **kw):
            seen["prompt"] = kw["messages"][1]["content"]
            raise RuntimeError("stop after capturing the prompt")

    p = OpenAICompatibleProposer(model="m", api_key="k", client=Fake())
    ctx = Context(task="t", sop="s", snapshot="page", denied=["disburse_x @ /pay/R-1 (amount=5)"])
    try:
        p.propose_actions(ctx)
    except RuntimeError:
        pass
    assert "DENIED BY A HUMAN (final; never propose these again):\n- disburse_x @ /pay/R-1 (amount=5)" in seen["prompt"]
