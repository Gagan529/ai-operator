"""The `assign` tool: give items limited slots, most important first. Pure computation.

Input: items (id, priority, allowed slots) and slots (id, order). Higher priority =
more important; lower order = earlier slot. Equal priorities keep the input order.

Two passes, both built on one question: "can this set of items all get a slot at once?"
(a bipartite matching, answered with augmenting paths: give the new item a free slot,
or move the item holding one of its slots to another of that item's slots, and so on).
  1. Who gets in: take items by priority; keep one only if all kept items still fit.
  2. Which slot: in priority order, give each kept item its earliest slot that still
     leaves a slot for every kept item after it.
The sets of items that can be placed together form a matroid (a transversal matroid),
so pass 1 is the matroid greedy: no other choice keeps a more important item that
this one leaves out. tests/test_assign.py checks it against brute force.

Nothing here knows what the items or slots are.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

MAX_ITEMS = 100
MAX_SLOTS = 200


class AssignItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    priority: float = Field(0, description="higher = more important")
    slots: list[str] = Field(description="ids of the slots this item may take")


class AssignSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    order: float = Field(0, description="lower = earlier; equal orders keep the input order")


class AssignResult(BaseModel):
    assigned: dict[str, str]            # item id -> slot id, in priority order
    unassigned: dict[str, str]          # item id -> why it got no slot

    def summary(self) -> str:
        """One line for the model's notes."""
        got = ", ".join(f"{i}->{s}" for i, s in self.assigned.items()) or "none"
        left = "; ".join(f"{i}: {why}" for i, why in self.unassigned.items())
        return f"assign result: {got}" + (f". Not placed: {left}" if left else "")


def assign(items: list[AssignItem], slots: list[AssignSlot]) -> AssignResult:
    """Raises ValueError for input the model must fix (duplicates, unknown slot ids, too large)."""
    _check(items, slots)
    rank = {s.id: (s.order, i) for i, s in enumerate(slots)}
    # allowed slots of each item, earliest first, duplicates dropped
    allowed = {it.id: sorted(set(it.slots), key=rank.__getitem__) for it in items}
    by_priority = [it.id for it in sorted(items, key=lambda it: -it.priority)]   # stable for ties

    # pass 1: who gets in
    kept: list[str] = []
    unassigned: dict[str, str] = {}
    match: dict[str, str] = {}          # slot -> item
    for item in by_priority:
        if not allowed[item]:
            unassigned[item] = "no allowed slots"
        elif _augment(item, allowed, match, set()):
            kept.append(item)
        else:
            unassigned[item] = "every slot it may take is needed by more important items"

    # pass 2: earliest slot for each kept item that still lets the rest fit
    fixed: dict[str, str] = {}          # item -> slot
    for n, item in enumerate(kept):
        rest = kept[n + 1:]
        taken = set(fixed.values())
        for slot in allowed[item]:
            if slot not in taken and _fits(rest, allowed, taken | {slot}):
                fixed[item] = slot
                break
        else:                           # cannot happen: pass 1 proved `kept` fits
            raise AssertionError(f"no slot left for {item}")
    return AssignResult(assigned=fixed, unassigned=unassigned)


def _augment(item: str, allowed: dict[str, list[str]], match: dict[str, str], seen: set[str],
             blocked: frozenset[str] = frozenset()) -> bool:
    """Find `item` a slot, moving already-placed items along an augmenting path if needed.
    On failure `match` is left unchanged."""
    for slot in allowed[item]:
        if slot in blocked or slot in seen:
            continue
        seen.add(slot)
        holder = match.get(slot)
        if holder is None or _augment(holder, allowed, match, seen, blocked):
            match[slot] = item
            return True
    return False


def _fits(items: list[str], allowed: dict[str, list[str]], blocked: set[str]) -> bool:
    """Can all `items` get distinct slots, without using any `blocked` slot?"""
    match: dict[str, str] = {}
    frozen = frozenset(blocked)
    return all(_augment(item, allowed, match, set(), frozen) for item in items)


def _check(items: list[AssignItem], slots: list[AssignSlot]) -> None:
    if len(items) > MAX_ITEMS or len(slots) > MAX_SLOTS:
        raise ValueError(f"assign takes at most {MAX_ITEMS} items and {MAX_SLOTS} slots")
    for kind, ids in (("item", [i.id for i in items]), ("slot", [s.id for s in slots])):
        dupes = sorted({x for x in ids if ids.count(x) > 1})
        if dupes:
            raise ValueError(f"duplicate {kind} ids: {', '.join(dupes)}")
    known = {s.id for s in slots}
    unknown = sorted({s for it in items for s in it.slots if s not in known})
    if unknown:
        raise ValueError(f"items name slots that are not in the slots list: {', '.join(unknown)}")
