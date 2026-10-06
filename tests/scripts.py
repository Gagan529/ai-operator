"""Scripted steps for the tests. The tests run on the frozen v1 dataset
(tests/fixtures/acme_seed_v1.json: R-12, A-101..A-106, S-1..S-5), not on the company's current data."""
from aiop.company import Company
from aiop.models import ProposedAction as A

_m = Company.load("acme_talent").module("scripted")
set_status, book, DONE = _m.set_status, _m.book, _m.DONE


def screening() -> list:
    """The v1 outcome: 3 shortlisted and booked by experience, 3 not a fit."""
    steps = []
    for cid in ["A-101", "A-103", "A-105"]:
        steps += set_status(cid, "Shortlisted", "meets all must-haves")
    for cid in ["A-102", "A-104", "A-106"]:
        steps += set_status(cid, "Not a fit (pending approval)", "missing must-haves")
    steps += book("S-1", "A-105") + book("S-3", "A-103") + book("S-4", "A-101")
    return steps
