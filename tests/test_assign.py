"""The assign solver against brute force, plus the edge cases the model can hit."""
import itertools
import random

import pytest

from aiop.assign import AssignItem as I, AssignSlot as S, _numbers, assign


def brute_force(items, slots):
    """Try every assignment. Best = keep the most important items first (compared item by item
    in priority order), then give each, in the same order, the earliest slot."""
    order = sorted(range(len(items)), key=lambda k: [-x for x in _numbers(items[k].priority)])
    rank = {s.id: (s.order, i) for i, s in enumerate(slots)}
    best, best_key = None, None
    for choice in itertools.product(*[[None, *sorted(set(it.slots))] for it in items]):
        used = [c for c in choice if c is not None]
        if len(used) != len(set(used)):
            continue
        key = (tuple(choice[k] is None for k in order),
               tuple(rank[choice[k]] if choice[k] else (0, 0) for k in order))
        if best_key is None or key < best_key:
            best, best_key = choice, key
    return {items[k].id: best[k] for k in order if best[k] is not None}


def random_case(rng):
    slots = [S(id=f"T{j}", order=rng.choice([1, 2, 3, 4, 5])) for j in range(rng.randint(1, 5))]
    items = [I(id=f"X{k}", priority=rng.choice([1, 2, 3, 5, 8]),
               slots=rng.sample([s.id for s in slots], rng.randint(0, len(slots))))
             for k in range(rng.randint(1, 6))]
    return items, slots


def test_matches_brute_force_on_random_cases():
    rng = random.Random(7)
    for _ in range(600):
        items, slots = random_case(rng)
        got = assign(items, slots)
        assert got.assigned == brute_force(items, slots), (items, slots)
        assert set(got.assigned) | set(got.unassigned) == {it.id for it in items}


def test_list_priorities_match_brute_force():
    """A company tie-break, e.g. [years, -application number], compared left to right."""
    rng = random.Random(3)
    for _ in range(300):
        items, slots = random_case(rng)
        for it in items:
            it.priority = [rng.choice([1, 2, 3]), rng.choice([-2, -1])]
        assert assign(items, slots).assigned == brute_force(items, slots)


def test_kept_set_has_the_highest_total_priority():
    """The matroid property: greedy also maximises the summed priority of the placed items."""
    rng = random.Random(11)
    for _ in range(300):
        items, slots = random_case(rng)
        got = assign(items, slots)
        weight = {it.id: it.priority for it in items}
        best = max(sum(weight[i] for i in m) for m in _all_matchable_sets(items))
        assert sum(weight[i] for i in got.assigned) == best


def _all_matchable_sets(items):
    for choice in itertools.product(*[[None, *it.slots] for it in items]):
        used = [c for c in choice if c is not None]
        if len(used) == len(set(used)):
            yield [it.id for it, c in zip(items, choice) if c is not None]


def test_naive_earliest_first_would_drop_an_item():
    """Taking the earliest free slot in priority order would give P the 9:00 slot and leave Q nothing."""
    slots = [S(id="9:00", order=1), S(id="10:00", order=2), S(id="11:00", order=3)]
    items = [I(id="P", priority=3, slots=["9:00", "10:00"]), I(id="Q", priority=2, slots=["9:00"]),
             I(id="R", priority=1, slots=["10:00", "11:00"])]
    got = assign(items, slots)
    assert got.assigned == {"P": "10:00", "Q": "9:00", "R": "11:00"} and not got.unassigned


def test_infeasible_item_is_reported_not_lost():
    slots = [S(id="T1", order=1), S(id="T2", order=2)]
    items = [I(id="A", priority=3, slots=["T1"]), I(id="B", priority=2, slots=["T1"]),
             I(id="C", priority=1, slots=["T2"]), I(id="D", priority=9, slots=[])]
    got = assign(items, slots)
    assert got.assigned == {"A": "T1", "C": "T2"}
    assert set(got.unassigned) == {"B", "D"} and "no allowed slots" in got.unassigned["D"]
    assert "B" in got.summary() and "A->T1" in got.summary()


def test_ties_keep_input_order():
    slots = [S(id="T1", order=1), S(id="T2", order=2)]
    items = [I(id="first", priority=3, slots=["T1", "T2"]), I(id="second", priority=3, slots=["T1", "T2"])]
    assert assign(items, slots).assigned == {"first": "T1", "second": "T2"}
    slots = [S(id="late"), S(id="early")]            # equal orders: the slots list order decides
    assert assign([I(id="x", slots=["early", "late"])], slots).assigned == {"x": "late"}


def test_more_items_than_slots():
    slots = [S(id=f"T{j}", order=j) for j in range(3)]
    items = [I(id=f"X{k}", priority=k, slots=[s.id for s in slots]) for k in range(5)]
    got = assign(items, slots)
    assert got.assigned == {"X4": "T0", "X3": "T1", "X2": "T2"} and set(got.unassigned) == {"X1", "X0"}


@pytest.mark.parametrize("items,slots,msg", [
    ([I(id="A", slots=["T9"])], [S(id="T1")], "not in the slots list"),
    ([I(id="A", slots=[]), I(id="A", slots=[])], [S(id="T1")], "duplicate item"),
    ([I(id="A", slots=[])], [S(id="T1"), S(id="T1")], "duplicate slot"),
])
def test_bad_input_is_rejected_with_a_reason(items, slots, msg):
    with pytest.raises(ValueError, match=msg):
        assign(items, slots)


def test_tie_that_decides_the_outcome_is_flagged():
    slots = [S(id="T1", order=1)]
    items = [I(id="A", priority=3, slots=["T1"]), I(id="B", priority=3, slots=["T1"])]
    got = assign(items, slots)
    assert got.assigned == {"A": "T1"} and got.ties == [["A", "B"]]
    assert "TIE decided only by input order" in got.summary()


def test_tie_that_changes_nothing_is_not_flagged():
    slots = [S(id="T1", order=1), S(id="T2", order=2)]
    items = [I(id="A", priority=3, slots=["T1"]), I(id="B", priority=3, slots=["T2"])]
    assert assign(items, slots).ties == []


def test_company_tie_break_settles_the_tie():
    slots = [S(id="T1", order=1), S(id="T2", order=2)]
    items = [I(id="A", priority=[3, -5], slots=["T1", "T2"]), I(id="B", priority=[3, -2], slots=["T1", "T2"])]
    got = assign(items, slots)                       # B applied earlier (no. 2 before no. 5)
    assert got.assigned == {"B": "T1", "A": "T2"} and got.ties == []


def test_priorities_of_different_lengths_are_refused():
    with pytest.raises(ValueError, match="same length"):
        assign([I(id="A", priority=[1, 2], slots=[]), I(id="B", priority=1, slots=[])], [S(id="T1")])
