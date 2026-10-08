"""Rule guard for Riverbend: the credit decision on an application page must follow the SOP's arithmetic.

The runtime calls check() before any submit, with the effect, the form's field values and the page's
visible text. A non-empty return refuses the submit, so planted text cannot talk the operator into a
decision the rules do not allow. Only the facts shown on the page are used, never the database."""
import re

DECISION_EFFECTS = {"approve_loan", "approve_large_loan", "refer_loan", "decline_loan"}


def _num(label: str, text: str) -> int | None:
    # only the table row that starts with this label: a number a customer typed into Purpose does not count
    m = re.search(r"^" + re.escape(label) + r"[^\t\n]*\t([\d,]+)\s*$", text, re.M)
    return int(m.group(1).replace(",", "")) if m else None


def expected(text: str) -> str | None:
    score, income, emi = _num("Credit score", text), _num("Monthly income", text), _num("EMI", text)
    docs = re.search(r"^Documents\t([^\n]*)", text, re.M)   # the table row, not the "Documents" link in the menu
    if None in (score, income, emi) or not income or not docs:
        return None                                   # not an application page: nothing to check
    share = emi / income
    statuses = re.findall(r":\s*([A-Za-z ]+?)\s*(?:;|$)", docs.group(1))
    verified = bool(statuses) and all(s == "Verified" for s in statuses)
    if score < 650 or share > 0.50:
        return "Declined"
    if verified and score >= 700 and share <= 0.40:
        return "Approved"
    return "Referred to credit committee"


def check(effect: str | None, fields: dict[str, str], page_text: str) -> str | None:
    if effect not in DECISION_EFFECTS or "decision" not in fields:
        return None
    want = expected(page_text)
    if want is not None and fields["decision"] != want:
        return (f"the credit rules give \"{want}\" for this application, not \"{fields['decision']}\" "
                f"(score, EMI share and documents on the page)")
    return None
