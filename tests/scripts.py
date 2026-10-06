"""The company's scripted replay, shared by the tests."""
from aiop.company import Company

_m = Company.load("acme_talent").module("scripted")
set_status, book, screening, DONE = _m.set_status, _m.book, _m.screening, _m.DONE
