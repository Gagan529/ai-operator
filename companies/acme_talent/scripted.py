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


# Shortlisted R-21 candidates by [years, -application no.] and Ritu Sharma's free slots by start time.
PLACE = A(tool="assign", reason="order interviews by experience, earlier application first on a tie",
          items=[{"id": cid, "priority": [years, -no], "slots": ["S-11", "S-13", "S-16", "S-17", "S-18"]}
                 for cid, years, no in [("A-201", 6, 3), ("A-203", 3, 7), ("A-206", 7, 5), ("A-207", 3, 2)]],
          slots=[{"id": s, "order": n} for n, s in enumerate(["S-11", "S-13", "S-16", "S-17", "S-18"])])


def screening() -> list:
    """The SOP outcome for the current data (R-21): 4 shortlisted and booked by experience, 4 not a fit."""
    steps = []
    for cid in ["A-201", "A-203", "A-206", "A-207"]:
        steps += set_status(cid, "Shortlisted", "meets all must-haves")
    for cid in ["A-202", "A-204", "A-205", "A-208"]:
        steps += set_status(cid, "Not a fit (pending approval)", "missing must-haves")
    steps += [PLACE]
    steps += book("S-11", "A-206") + book("S-13", "A-201") + book("S-16", "A-207") + book("S-17", "A-203")
    return steps


DONE = A(tool="done", reason="all applicants screened and interviews booked")


def demo() -> list:
    """Console demo: the correct screening, then one rejection that needs a human."""
    return screening() + set_status("A-208", "Rejected", "1 year only; recruiter to confirm") + [DONE]
