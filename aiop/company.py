"""Loads one company's folder: metadata, SOP, policies and checks.

`aiop/` never names a company. Everything company-specific comes from here.
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPANIES = ROOT / "companies"


@dataclass
class Company:
    id: str
    name: str
    folder: Path
    origins: list[str]
    start_url: str
    db_path: Path
    sop: str
    policies: dict[str, Any]
    invariants: list[dict[str, Any]]       # checked after any task
    goals: dict[str, list[dict[str, Any]]] # task text (normalised) -> goal checks for that task
    default_task: str
    demo: dict[str, Any] = field(default_factory=dict)

    def goal_checks(self, task: str) -> list[dict[str, Any]] | None:
        """The goal checks for this exact task, or None: an unknown task cannot be VERIFIED."""
        return self.goals.get(_norm(task))

    def module(self, name: str) -> ModuleType | None:
        """An optional Python file in the company folder, e.g. scripted.py for offline replays."""
        path = self.folder / f"{name}.py"
        if not path.exists():
            return None
        spec = importlib.util.spec_from_file_location(f"companies.{self.id}.{name}", path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    @classmethod
    def load(cls, company_id: str, origins: list[str] | None = None) -> Company:
        """`origins` overrides company.yaml, e.g. when tests serve the app on another port."""
        folder = COMPANIES / company_id
        meta = _yaml(folder / "company.yaml")
        checks = _yaml(folder / meta["checks"])
        default_task = " ".join(meta.get("default_task", "").split())
        goals = {_norm(default_task if task == "default" else task): list(cs)
                 for task, cs in (checks.get("goals") or {}).items()}
        return cls(
            id=meta["id"],
            name=meta["name"],
            folder=folder,
            origins=origins or meta["origins"],
            start_url=meta.get("start_url", "/"),
            db_path=ROOT / meta["db"],
            sop=(folder / meta["sop"]).read_text(encoding="utf-8"),
            policies=_yaml(folder / meta["policies"]),
            invariants=checks.get("invariants") or [],
            goals=goals,
            default_task=default_task,
            demo=meta.get("demo") or {},
        )


def _norm(task: str) -> str:
    return " ".join(task.split()).lower()


def _yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
