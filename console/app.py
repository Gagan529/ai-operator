"""Operator console: start a run, watch every step, approve or deny, see the verdict and evidence.

Run:  python -m console.app        -> http://127.0.0.1:8000
It also serves each company's mock app (company.yaml: demo.mock_app), so one command is enough.

The console is only a window onto the runtime. It never decides anything: approvals
go into the run through LangGraph's resume, exactly like the tests.
"""

import importlib
import json
import queue
import socket
import threading
import time
import traceback
import urllib.request
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlencode, urlparse

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from langgraph.types import Command

from aiop import graph
from aiop.browser import Browser
from aiop.company import COMPANIES, Company
from aiop.evidence import RUNS, EvidenceStore
from aiop.llm import OpenAICompatibleProposer, ScriptedProposer
from aiop.models import RunStatus
from aiop.runtime import Runtime

STATIC = Path(__file__).parent / "static"
CHECKPOINTS = RUNS / "checkpoints.db"


class StartRun(BaseModel):
    company: str = "acme_talent"
    task: str | None = None
    proposer: Literal["live", "scripted"] = "scripted"
    flaky: bool = False
    silent_fail: bool = False
    injection: bool = False
    headed: bool = False


class Answer(BaseModel):
    approval_id: str                 # which approval request this answers; a stale answer is refused
    approved: bool
    note: str = ""


class Worker:
    """Owns the browser and the graph. Playwright's sync API must stay on one thread,
    so web handlers only queue jobs here and read the latest state."""

    def __init__(self) -> None:
        self.jobs: queue.Queue[tuple[Callable[..., None], tuple]] = queue.Queue()
        self.runs: dict[str, dict[str, Any]] = {}
        self.busy = False
        self._browser: Browser | None = None
        self._app = None
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, fn: Callable[..., None], *args) -> None:
        self.busy = True
        self.jobs.put((fn, args))

    def _loop(self) -> None:
        while True:
            fn, args = self.jobs.get()
            try:
                fn(*args)
            except Exception as e:  # shown in the console, never swallowed
                self.runs.setdefault(args[0], {})["error"] = f"{type(e).__name__}: {e}"
                traceback.print_exc()
            finally:
                self.busy = not self.jobs.empty()

    def start(self, run_id: str, req: StartRun) -> None:
        company = Company.load(req.company)
        reset_mock_app(company, req)
        if self._browser:
            self._browser.close()
        self._browser = Browser(company.origins, headless=not req.headed).start()
        if req.proposer == "scripted":
            script = company.module("scripted")
            if script is None:
                raise RuntimeError(f"{company.id} has no scripted.py")
            proposer = ScriptedProposer(script.demo(), plan=["replay the recorded correct run"])
        else:
            proposer = OpenAICompatibleProposer()
        state = Runtime.new_state(company, req.task, run_id=run_id)
        rt = Runtime(company, self._browser, proposer, EvidenceStore(run_id))
        self._app = graph.build(rt, graph.sqlite_checkpointer(CHECKPOINTS))
        self._drive(run_id, {"run": state.model_dump(mode="json")})

    def answer(self, run_id: str, a: Answer) -> None:
        if (graph.pending(self._app, run_id) or {}).get("approval_id") != a.approval_id:
            return                   # queued twice, or that request was already answered
        self.runs[run_id]["interrupt"] = None
        self._drive(run_id, Command(resume={"approval_id": a.approval_id, "approved": a.approved,
                                            "by": "console user", "note": a.note}))

    def _drive(self, run_id: str, payload: Any) -> None:
        entry = self.runs.setdefault(run_id, {})
        for run in graph.stream(self._app, payload, run_id):
            entry["state"] = run
        entry["interrupt"] = graph.pending(self._app, run_id)


worker = Worker()
app = FastAPI(title="ai-operator console")
RUNS.mkdir(exist_ok=True)
app.mount("/runs", StaticFiles(directory=RUNS), name="runs")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/companies")
def companies():
    out = []
    for folder in sorted(COMPANIES.iterdir()):
        if (folder / "company.yaml").exists():
            c = Company.load(folder.name)
            out.append({"id": c.id, "name": c.name, "task": c.default_task, "sop": c.sop,
                        "policies": (c.folder / "policies.yaml").read_text(encoding="utf-8"),
                        "checks": [ch["name"] for ch in c.invariants + (c.goal_checks(c.default_task) or [])],
                        "scripted": (c.folder / "scripted.py").exists()})
    return out


@app.post("/api/runs")
def start_run(req: StartRun):
    if req.company not in {f.name for f in COMPANIES.iterdir() if (f / "company.yaml").exists()}:
        raise HTTPException(404, "unknown company")      # also stops names like ../../x reaching the filesystem
    if worker.busy:
        raise HTTPException(409, "a run is still working; wait for it to finish or pause")
    run_id = Runtime.new_state(Company.load(req.company)).run_id
    worker.runs[run_id] = {"state": None, "interrupt": None, "error": None, "request": req.model_dump()}
    worker.submit(worker.start, run_id, req)
    return {"run_id": run_id}


@app.post("/api/runs/{run_id}/answer")
def answer(run_id: str, a: Answer):
    entry = worker.runs.get(run_id)
    if not entry or not entry.get("interrupt"):
        raise HTTPException(409, "this run is not waiting for approval")
    if entry["interrupt"].get("approval_id") != a.approval_id:
        raise HTTPException(409, "this answer is for a different approval request")
    worker.submit(worker.answer, run_id, a)
    return {"ok": True}


@app.get("/api/runs/{run_id}")
def run_view(run_id: str, since: int = 0):
    entry = worker.runs.get(run_id)
    if entry is None:
        raise HTTPException(404, "unknown run")
    state = entry.get("state")
    events = [e.model_dump() for e in EvidenceStore(run_id).events()]
    verification = None
    if state and state.get("verification_ref"):
        verification = json.loads((RUNS / run_id / state["verification_ref"]).read_text(encoding="utf-8"))
    finished = bool(state) and state["status"] in (RunStatus.VERIFIED, RunStatus.UNVERIFIED, RunStatus.FAILED, RunStatus.STOPPED)
    return {"state": state, "events": events[since:], "total_events": len(events),
            "interrupt": entry.get("interrupt"), "error": entry.get("error"), "busy": worker.busy,
            "verification": verification, "finished": finished,
            "report": f"/runs/{run_id}/report.html" if (RUNS / run_id / "report.html").exists() else None}


# ---------- local demo wiring ----------
def reset_mock_app(company: Company, req: StartRun) -> None:
    """Fresh data + fault switches before each run, through the app's machine endpoint."""
    path = company.demo.get("reset")
    if not path:
        return
    q = urlencode({"flaky": req.flaky, "silent_fail": req.silent_fail, "injection": req.injection}).lower()
    urllib.request.urlopen(urllib.request.Request(f"{company.origins[0]}{path}?{q}", method="POST"), timeout=10)


def serve_mock_apps() -> None:
    for folder in sorted(COMPANIES.iterdir()):
        if not (folder / "company.yaml").exists():
            continue
        c = Company.load(folder.name)
        target = c.demo.get("mock_app")
        if not target:
            continue
        u = urlparse(c.origins[0])
        if _port_open(u.hostname or "127.0.0.1", u.port or 80):
            print(f"[console] {c.id}: mock app already running at {c.origins[0]}")
            continue
        module, attr = target.split(":")
        mock = getattr(importlib.import_module(module), attr)
        srv = uvicorn.Server(uvicorn.Config(mock, host=u.hostname, port=u.port, log_level="warning"))
        threading.Thread(target=srv.run, daemon=True).start()
        while not srv.started:
            time.sleep(0.05)
        print(f"[console] {c.id}: mock app at {c.origins[0]}")


def _port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


if __name__ == "__main__":
    serve_mock_apps()
    print("[console] http://127.0.0.1:8000")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
