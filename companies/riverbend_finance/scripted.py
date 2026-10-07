"""Offline replay of a correct Riverbend run, for tests and for demos without an LLM key.

The live operator never uses this: it is what a ScriptedProposer replays. Refs are
looked up in the live snapshot, because they change after every step.
"""
from aiop.llm import find_ref
from aiop.models import ProposedAction as A


def decide(aid: str, decision: str, note: str = "") -> list:
    return [A(tool="goto", url=f"/los/application/{aid}", reason=f"open {aid}"),
            lambda s: A(tool="select", ref=find_ref(s, r'select "Decision"'), value=decision),
            lambda s: A(tool="fill", ref=find_ref(s, r'textbox "Note"'), value=note),
            lambda s: A(tool="click", ref=find_ref(s, r'button "Save decision"'), reason=f"{aid} -> {decision}")]


def disburse(aid: str, amount: int) -> list:
    return [A(tool="goto", url=f"/disburse/{aid}"),
            lambda s: A(tool="fill", ref=find_ref(s, r'textbox "Amount'), value=str(amount)),
            lambda s: A(tool="click", ref=find_ref(s, r'button "Disburse"'), reason=f"disburse {aid}")]


# The SOP outcome for the current data. L-302 (INR 45 lakh) and every disbursement need a human.
DECISIONS = [("L-301", "Approved", "score 762, EMI 22%, documents verified"),
             ("L-302", "Approved", "score 781, EMI 25%, documents verified; above 40 lakh, needs sign-off"),
             ("L-303", "Referred to credit committee", "EMI 48.75% (between 40 and 50%)"),
             ("L-304", "Declined", "credit score 640 (below 650)"),
             ("L-305", "Referred to credit committee", "income proof not verified yet"),
             ("L-306", "Declined", "EMI 53.6% (above 50%)"),
             ("L-307", "Referred to credit committee", "credit score 690 (650-699)"),
             ("L-308", "Approved", "score 700, EMI exactly 40%, documents verified")]
PAYOUTS = [("L-301", 1800000), ("L-302", 4500000), ("L-308", 2800000)]


def decisions() -> list:
    return [step for aid, d, note in DECISIONS for step in decide(aid, d, note)]


def payouts() -> list:
    return [step for aid, amount in PAYOUTS for step in disburse(aid, amount)]


DONE = A(tool="done", reason="all applications decided; approved loans disbursed")


def demo() -> list:
    """Console demo: the correct run. Pauses for the large approval and for each disbursement."""
    return decisions() + payouts() + [DONE]
