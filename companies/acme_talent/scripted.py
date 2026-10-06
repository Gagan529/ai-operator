"""Offline replay of a correct Acme Talent run, for tests and for demos without an LLM key.

The live operator never uses this: it is what a ScriptedProposer replays. Refs are
looked up in the live snapshot, because they change after every step.
"""
from aiop.llm import find_ref
from aiop.models import ProposedAction as A


def set_status(cid: str, status: str, note: str = "") -> list:
    return [A(tool="goto", url=f"/ats/candidate/{cid}", reason=f"open {cid}"),
            lambda s: A(tool="select", ref=find_ref(s, r'select "Status"'), value=status),
            lambda s: A(tool="fill", ref=find_ref(s, r'textbox "Note"'), value=note),
            lambda s: A(tool="click", ref=find_ref(s, r'button "Save"'), reason=f"{cid} -> {status}")]


def book(slot: str, cid: str) -> list:
    return [A(tool="goto", url="/calendar"),
            lambda s: A(tool="fill", ref=find_ref(s, r'textbox "Slot ID"'), value=slot),
            lambda s: A(tool="fill", ref=find_ref(s, r'textbox "Candidate ID"'), value=cid),
            lambda s: A(tool="click", ref=find_ref(s, r'button "Book"'), reason=f"book {slot} for {cid}")]


def screening() -> list:
    """The SOP outcome: 3 shortlisted and booked by experience, 3 not a fit."""
    steps = []
    for cid in ["A-101", "A-103", "A-105"]:
        steps += set_status(cid, "Shortlisted", "meets all must-haves")
    for cid in ["A-102", "A-104", "A-106"]:
        steps += set_status(cid, "Not a fit (pending approval)", "missing must-haves")
    steps += book("S-1", "A-105") + book("S-3", "A-103") + book("S-4", "A-101")
    return steps


DONE = A(tool="done", reason="all applicants screened and interviews booked")


def demo() -> list:
    """Console demo: the correct screening, then one rejection that needs a human."""
    return screening() + set_status("A-106", "Rejected", "no industry experience; recruiter to confirm") + [DONE]
