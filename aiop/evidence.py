"""Evidence store: everything large about a run lives in runs/<run_id>/.

The checkpoint keeps only event IDs and file paths. Layout:
  timeline.jsonl        one EvidenceEvent per line (append-only, survives resume)
  observations/NNN.txt  full snapshots
  screenshots/NNN-*.png
  db_before.json, db_after.json, db_diff.json, verification.json
  report.html           a readable summary of the run
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from aiop.models import EvidenceEvent, RunState, VerificationResult

RUNS = Path(__file__).resolve().parent.parent / "runs"


def _usage_html(u: dict[str, Any] | None) -> str:
    if not u:
        return ""
    return ("<h2>LLM usage</h2><p>"
            f"{u['calls']} calls · input per call min {u['in_min']} / avg {u['in_avg']} / max {u['in_max']} tokens "
            f"(bounded context: it stays flat instead of growing with the run) · cache hits {u['cached']} tokens "
            f"({u['cache_hit_pct']}%) · reasoning {u['reasoning']} of {u['out']} output tokens · "
            f"avg {u['avg_ms']} ms per call</p>")


class EvidenceStore:
    def __init__(self, run_id: str, root: Path = RUNS):
        self.run_id = run_id
        self.dir = root / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._timeline = self.dir / "timeline.jsonl"

    def add(self, step: int, kind: str, summary: str, data: dict[str, Any] | None = None,
            artifact: str | None = None) -> str:
        ev = EvidenceEvent(id=f"ev-{self._count() + 1:04d}", step=step, kind=kind, summary=summary,
                           data=data or {}, artifact=artifact)
        with self._timeline.open("a", encoding="utf-8") as f:
            f.write(ev.model_dump_json() + "\n")
        return ev.id

    def events(self) -> list[EvidenceEvent]:
        if not self._timeline.exists():
            return []
        out = []
        for line in self._timeline.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(EvidenceEvent.model_validate_json(line))
                except ValueError:      # a line still being written, or cut off by a killed process
                    continue
        return out

    def save_text(self, rel: str, text: str) -> str:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return rel

    def append_jsonl(self, rel: str, obj: Any) -> str:
        with self.path(rel).open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, default=str) + "\n")
        return rel

    def usage_summary(self) -> dict[str, Any] | None:
        """Per-call input size stays flat when context is bounded; the report shows it."""
        p = self.dir / "llm_usage.jsonl"
        if not p.exists():
            return None
        rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not rows:
            return None
        ins = [r["in"] for r in rows]
        total_in = sum(ins)
        cached = sum(r["cached"] for r in rows)
        return {"calls": len(rows), "in": total_in, "cached": cached,
                "out": sum(r["out"] for r in rows), "reasoning": sum(r["reasoning"] for r in rows),
                "in_min": min(ins), "in_avg": round(total_in / len(rows)), "in_max": max(ins),
                "cache_hit_pct": round(100 * cached / total_in, 1) if total_in else 0.0,
                "avg_ms": round(sum(r.get("ms", 0) for r in rows) / len(rows))}

    def save_json(self, rel: str, obj: Any) -> str:
        return self.save_text(rel, json.dumps(obj, indent=2, default=str))

    def path(self, rel: str) -> Path:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def _count(self) -> int:
        if not self._timeline.exists():
            return 0
        with self._timeline.open(encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())

    # ---------- end-of-run artefacts ----------
    def db_diff(self, before: dict[str, list[dict]], after: dict[str, list[dict]]) -> dict[str, Any]:
        """Changed rows per table, keyed by the first column (tables without a key are compared as lists)."""
        diff: dict[str, Any] = {}
        for table in sorted(set(before) | set(after)):
            b, a = before.get(table, []), after.get(table, [])
            if not (b or a):
                continue
            key = next(iter((b or a)[0]))
            bm, am = {r[key]: r for r in b}, {r[key]: r for r in a}
            if len(bm) != len(b) or len(am) != len(a):   # no unique key: report added rows only
                added = a[len(b):]
                if added:
                    diff[table] = {"added": added}
                continue
            changed = {k: {"before": bm.get(k), "after": am.get(k)} for k in sorted(set(bm) | set(am), key=str)
                       if bm.get(k) != am.get(k)}
            if changed:
                diff[table] = changed
        return diff

    def write_report(self, state: RunState, verification: VerificationResult | None) -> str:
        rows = "".join(
            f"<tr><td>{e.step}</td><td>{e.kind}</td><td>{html.escape(e.summary)}</td>"
            f"<td>{f'<a href={html.escape(e.artifact)}>{html.escape(e.artifact)}</a>' if e.artifact else ''}</td></tr>"
            for e in self.events())
        checks = "" if verification is None else "".join(
            f"<tr><td>{'✅' if c.passed else '❌'}</td><td>{c.kind}</td><td>{html.escape(c.name)}</td>"
            f"<td>{html.escape(str(c.expected))}</td><td>{html.escape(str(c.actual))}</td></tr>"
            for c in verification.checks)
        doc = (f"<!doctype html><meta charset=utf-8><title>Run {state.run_id}</title>"
               "<style>body{font-family:sans-serif;max-width:1100px;margin:20px auto}"
               "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:3px 6px;vertical-align:top}</style>"
               f"<h1>Run {state.run_id}: {state.status.value}</h1>"
               f"<p><b>Company:</b> {html.escape(state.company)} · <b>Task:</b> {html.escape(state.task)}</p>"
               f"<p><b>Verification:</b> {state.verification.value if state.verification else 'not run'}"
               f" · <b>Stop reason:</b> {html.escape(state.stop_reason or '-')}"
               f" · <b>Tokens:</b> {state.tokens_in} in ({state.tokens_cached} cached) / {state.tokens_out} out"
               f" ({state.tokens_reasoning} reasoning) · <b>LLM calls:</b> {state.llm_calls}</p>"
               + _usage_html(self.usage_summary()) +
               f"<h2>Verification checks</h2><table><tr><th></th><th>Kind</th><th>Check</th><th>Expected</th><th>Actual</th></tr>{checks}</table>"
               f"<h2>Timeline</h2><table><tr><th>Step</th><th>Kind</th><th>What happened</th><th>Artefact</th></tr>{rows}</table>")
        return self.save_text("report.html", doc)
