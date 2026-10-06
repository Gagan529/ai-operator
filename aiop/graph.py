"""Thin LangGraph adapter. All behaviour lives in runtime.py; this file only wires it.

    START -> begin -> step --(RUNNING)--> step
                          --(AWAITING_APPROVAL)--> approval --interrupt()--> (human) --> step
                          --(VERIFIED / FAILED / STOPPED)--> END

Why LangGraph at all: every node boundary is checkpointed to SQLite, so a run
waiting for a human survives a restart, and `interrupt()` + `Command(resume=...)`
is the only door through which an approval can enter. The graph state is the
small `RunState` (as a dict); snapshots and screenshots stay in runs/<id>/.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from aiop.models import RunState, RunStatus
from aiop.runtime import Runtime


class GraphState(TypedDict):
    run: dict[str, Any]


def build(runtime: Runtime, checkpointer: SqliteSaver):
    def begin(s: GraphState) -> GraphState:
        state = RunState.model_validate(s["run"])
        return {"run": runtime.start(state).model_dump(mode="json")}

    def step(s: GraphState) -> GraphState:
        state = RunState.model_validate(s["run"])
        return {"run": runtime.step(state).model_dump(mode="json")}

    def approval(s: GraphState) -> GraphState:
        state = RunState.model_validate(s["run"])
        pending = state.pending_approval
        assert pending is not None
        # Nothing above this line may have side effects: the node re-runs on resume.
        answer = interrupt({"run_id": state.run_id, "effect": pending.decision.effect,
                           "reason": pending.decision.reason, "rule": pending.decision.rule,
                           "action": pending.action.reason or pending.action.tool,
                           "fields": pending.target.form_fields})
        answer = answer if isinstance(answer, dict) else {"approved": bool(answer)}
        runtime.resume(state, approved=bool(answer.get("approved")), by=str(answer.get("by", "human")),
                       note=str(answer.get("note", "")))
        return {"run": state.model_dump(mode="json")}

    def route(s: GraphState) -> str:
        status = s["run"]["status"]
        if status == RunStatus.RUNNING.value:
            return "step"
        if status == RunStatus.AWAITING_APPROVAL.value:
            return "approval"
        return END

    g = StateGraph(GraphState)
    g.add_node("begin", begin)
    g.add_node("step", step)
    g.add_node("approval", approval)
    g.add_edge(START, "begin")
    g.add_conditional_edges("begin", route, ["step", "approval", END])
    g.add_conditional_edges("step", route, ["step", "approval", END])
    g.add_conditional_edges("approval", route, ["step", "approval", END])
    return g.compile(checkpointer=checkpointer)


def sqlite_checkpointer(path: Path) -> SqliteSaver:
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))


def config(run_id: str, max_steps: int) -> dict[str, Any]:
    # each step is one graph node, so the recursion limit must cover the step budget
    return {"configurable": {"thread_id": run_id}, "recursion_limit": max_steps * 3 + 20}


def start(app, state: RunState) -> dict[str, Any]:
    return app.invoke({"run": state.model_dump(mode="json")}, config(state.run_id, state.budgets.max_steps))


def answer(app, run_id: str, approved: bool, by: str = "human", note: str = "",
           max_steps: int = 60) -> dict[str, Any]:
    return app.invoke(Command(resume={"approved": approved, "by": by, "note": note}), config(run_id, max_steps))


def stream(app, payload: Any, run_id: str, max_steps: int = 60):
    """Yield the run state after every node, for live views. `payload` is the first
    input ({"run": ...}) or a Command(resume=...)."""
    for values in app.stream(payload, config(run_id, max_steps), stream_mode="values"):
        if "run" in values:
            yield values["run"]


def pending(app, run_id: str) -> dict[str, Any] | None:
    """The open approval request, if the run is paused at interrupt()."""
    snap = app.get_state({"configurable": {"thread_id": run_id}})
    return snap.interrupts[0].value if snap.interrupts else None


def current(app, run_id: str) -> RunState:
    return RunState.model_validate(app.get_state({"configurable": {"thread_id": run_id}}).values["run"])
