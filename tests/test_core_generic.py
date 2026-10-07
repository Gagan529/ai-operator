"""The core must not know any company: no business words in aiop/ (they belong in companies/<id>/)."""
import re
from pathlib import Path

WORDS = r"candidat|recruit|interview|applicant|hiring|shortlist|loan|borrower|disburse"


def test_core_has_no_business_words():
    hits = [f"{p.name}:{n}: {line.strip()}"
            for p in sorted((Path(__file__).parent.parent / "aiop").glob("*.py"))
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if re.search(WORDS, line, re.I)]
    assert not hits, "\n".join(hits)
