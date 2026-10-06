"""Deterministic policy engine: judges what an action will really do.

The input is the proposed action plus the `Target` the browser resolved from
the live page. The LLM's own words (its `reason`) are never read here.

Effect resolution for a submit, in order:
  1. explicit `data-aiop-effect` on the control, and on selected options in its form
  2. fallback rules from policies.yaml (method + form action path + field values)
  3. none found -> defaults.unknown_submit (conservative)
Each effect maps to ALLOW / APPROVAL_REQUIRED / DENY; several effects -> the strictest.
"""
from __future__ import annotations

from fnmatch import fnmatch
from typing import Any
from urllib.parse import urlparse

from aiop.models import Decision, PolicyDecision, ProposedAction, Target

_STRICTNESS = {Decision.ALLOW: 0, Decision.APPROVAL_REQUIRED: 1, Decision.DENY: 2}


class PolicyEngine:
    def __init__(self, rules: dict[str, Any]):
        self.effects: dict[str, Any] = rules.get("effects", {})
        self.fallback: list[dict[str, Any]] = rules.get("fallback_rules", [])
        self.deny_paths: list[str] = rules.get("deny_paths", [])
        defaults = rules.get("defaults", {})
        self.unknown_effect = Decision(defaults.get("unknown_effect", "APPROVAL_REQUIRED"))
        self.unknown_submit = Decision(defaults.get("unknown_submit", "APPROVAL_REQUIRED"))

    def decide(self, action: ProposedAction, target: Target | None = None,
               current_url: str = "") -> PolicyDecision:
        if action.tool in ("read", "done"):
            return _d(Decision.ALLOW, None, "observe", "reading the page changes nothing")
        if action.tool == "goto":
            return self._navigation(urlparse(action.url or "").path)
        if action.tool in ("fill", "select"):
            # editing a form changes nothing until it is submitted; the submit is judged
            return _d(Decision.ALLOW, None, "form-input", "form edits take effect only on submit")
        # click
        if target is None:
            raise ValueError("click needs a resolved target")
        if target.href:
            return self._navigation(urlparse(target.href).path)
        if not target.is_submit:
            if target.effect:
                return self._for_effects([target.effect], "explicit-effect")
            return _d(Decision.ALLOW, None, "non-submit-click", "control does not submit a form")
        return self._submit(target)

    # ---------- internals ----------
    def _navigation(self, path: str) -> PolicyDecision:
        for pattern in self.deny_paths:
            if fnmatch(path, pattern):
                return _d(Decision.DENY, None, f"deny_paths:{pattern}", f"{path} is off-limits to the operator")
        return _d(Decision.ALLOW, None, "navigation", "opening a page changes nothing")

    def _submit(self, t: Target) -> PolicyDecision:
        explicit = [e for e in [t.effect, *t.form_effects] if e]
        if explicit:
            return self._for_effects(explicit, "explicit-effect")
        path = urlparse(t.form_action or "").path
        for rule in self.fallback:
            if rule.get("method", "post") != (t.form_method or "get"):
                continue
            if not fnmatch(path, rule["action"]):
                continue
            if all(t.form_fields.get(k) == v for k, v in (rule.get("fields") or {}).items()):
                return self._for_effects([rule["effect"]], f"fallback:{rule['name']}")
        return _d(self.unknown_submit, None, "defaults.unknown_submit",
                  f"submit to {path or '?'} has no known effect; defaulting conservatively")

    def _for_effects(self, effects: list[str], source: str) -> PolicyDecision:
        worst: PolicyDecision | None = None
        for effect in effects:
            spec = self.effects.get(effect)
            if spec is None:
                d = _d(self.unknown_effect, effect, f"{source}/defaults.unknown_effect",
                       f"effect '{effect}' is not in the policy file")
            elif isinstance(spec, str):
                d = _d(Decision(spec), effect, f"{source}/effects.{effect}", f"policy for '{effect}' is {spec}")
            else:
                d = _d(Decision(spec["decision"]), effect, f"{source}/effects.{effect}",
                       spec.get("reason", f"policy for '{effect}' is {spec['decision']}"))
            if worst is None or _STRICTNESS[d.decision] > _STRICTNESS[worst.decision]:
                worst = d
        assert worst is not None
        return worst


def _d(decision: Decision, effect: str | None, rule: str, reason: str) -> PolicyDecision:
    return PolicyDecision(decision=decision, effect=effect, rule=rule, reason=reason)
