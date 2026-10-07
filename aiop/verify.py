"""Independent verifier: the agent's claim of success is never proof of success.

Runs the company's checks.yaml queries against the app's own database, read-only,
after the agent says it is done. It never asks the LLM and never looks at the
page. This is a "verification adapter": here it reads SQLite; for a real company
the same interface would call the system's API.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from aiop.models import VerificationCheck, VerificationResult


class Verifier:
    def __init__(self, db_path: Path, invariants: list[dict[str, Any]]):
        self.db_path = db_path
        self.invariants = invariants

    def run(self, facts: dict[str, Any] | None = None,
            goals: list[dict[str, Any]] | None = None) -> VerificationResult:
        """Invariants always; goal checks when the task has them (None = this task has none, so the
        best possible outcome is UNVERIFIED). `facts` supplies run facts for `$name.key` expectations,
        e.g. {"approved": {"delete_record": 1}}."""
        facts = facts or {}
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            results = [self._one(con, c, facts, "invariant") for c in self.invariants]
            results += [self._one(con, c, facts, "goal") for c in goals or []]
        finally:
            con.close()
        return VerificationResult.from_checks(results, goal_checked=bool(goals))

    def snapshot_db(self) -> dict[str, list[dict[str, Any]]]:
        """Every table's rows, for the evidence store's before/after diff."""
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {t: [dict(r) for r in con.execute(f'SELECT * FROM "{t}"')] for t in tables}
        finally:
            con.close()

    def _one(self, con: sqlite3.Connection, check: dict[str, Any], facts: dict[str, Any],
             kind: str) -> VerificationCheck:
        expected = _resolve(check["expect"], facts)
        try:
            row = con.execute(check["sql"]).fetchone()
            actual = row[0] if row else None
        except sqlite3.Error as e:
            actual = f"query error: {e}"
        return VerificationCheck(name=check["name"], expected=expected, actual=actual,
                                 passed=_same(expected, actual), critical=check.get("critical", True), kind=kind,
                                 evidence=" ".join(check["sql"].split()))


def _resolve(expect: Any, facts: dict[str, Any]) -> Any:
    """`$approved.delete_record` -> facts["approved"].get("delete_record", 0)."""
    if isinstance(expect, str) and expect.startswith("$"):
        group, _, key = expect[1:].partition(".")
        return (facts.get(group) or {}).get(key, 0)
    return expect


def _same(expected: Any, actual: Any) -> bool:
    if expected is None or actual is None:
        return expected == actual
    return str(expected).strip() == str(actual).strip()
