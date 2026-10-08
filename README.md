# ai-operator

> **© 2026 Gagan Gupta. All rights reserved.** Shared solely for evaluation in CentrAlign AI's hiring process. No licence is granted to use, copy, modify or distribute this code. See [NOTICE](NOTICE).

An AI operator that does real work in a company's web apps through a browser, the way a new employee would: it
reads the SOP, opens the pages, fills the forms, asks a human when the policy says so, and then has its work
checked against the company's own database.

The idea behind it is one sentence: **the LLM proposes, the runtime decides.** A language model is good at
reading a page and working out the next step. It is not something you can trust with permissions, approvals or
the final word on whether the job got done. So in this project the model only ever proposes. Everything that
matters is decided by plain code: what an action will really do, whether it is allowed, whether a human has to
approve it, whether it worked, and whether the task as a whole is actually finished.

There are two fictional companies to show that the core is not tied to one workflow:

- **Acme Talent** (recruiting): screen applicants for a role against its must-haves, mark the rest as not a fit,
  and book interviews in the interviewer's free slots, most experienced first.
- **Riverbend Home Finance** (home loans): decide every pending application by the credit rules, get a credit
  officer's sign-off on large loans, and pay out the approved ones under maker-checker control without going over
  the day's funds.

The second company was added without changing a single file in `aiop/`.

---

## Quick start

You need Python 3.11+ and about 300 MB for Playwright's Chromium.

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows   (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env            # Linux/macOS: cp .env.example .env
```

Put an OpenAI-compatible endpoint, key and model in `.env`. I used OpenRouter's free
`nvidia/nemotron-3-super-120b-a12b:free`, so a run costs nothing; any other compatible provider works the same way.

```bash
python -m console.app             # http://127.0.0.1:8000
```

One command starts the operator console and both mock company apps (recruiting on :8001, loans on :8002). In the
console you pick a company, then either **Scripted** (a recorded correct run, no key needed) or **Live** (the LLM
drives it). Switches let you turn on the failure modes described below. When the policy asks for approval, an
approval card appears and you decide.

Tests (no key needed, they use a scripted proposer against the real apps and a real headless browser):

```bash
pytest -q
```

---

## How a run works

Take one step from the recruiting task: the model wants to reject a candidate.

1. **Propose.** The model sees the task, the SOP, its notes and a compact text snapshot of the current page, and
   replies with JSON, for example `submit_form` on `/ats/candidate/A-204` with `status = Rejected`. The schema forbids
   extra keys, so it cannot slip in an `"approved": true` or an `"effect"` field.
2. **Resolve.** The runtime opens that page itself, fills the form and finds the real submit button. From now on it
   works with what the page actually contains, not with what the model said.
3. **Policy.** The policy engine reads the effect of that real button. The apps tag consequential controls with
   `data-aiop-effect`, and for pages without tags there are fallback rules in `policies.yaml`. Here the effect is
   `reject_candidate`, which the company's `policies.yaml` maps to **APPROVAL_REQUIRED**.
4. **Approve.** The run pauses in a LangGraph `interrupt()`, checkpointed to SQLite, so it survives a restart. A
   human approves or denies in the console. On resume the runtime rebuilds the form and checks that the effect is
   still the one that was approved before it clicks anything.
5. **Act and observe.** The click happens, the page is read again, and any failure is put into a class:
   transient (retry with backoff), missing element, app error, policy violation, and so on. Each class has its own
   recovery move, with budgets: three failures in a row trigger a re-plan, and two re-plans end the run.
6. **Verify.** When the model says "done", that claim is checked, never trusted. The verifier runs the company's
   `checks.yaml` SQL against the app's own database: **invariants** that must hold after any task (no rejection
   without approval, no double-booked slot, no payout without verified documents) and **goal checks** for that
   specific task. A task without goal checks can at best end **UNVERIFIED**.
7. **Evidence.** Every step lands in `runs/<run_id>/`: a timeline, every page snapshot, screenshots, LLM usage, the
   database before and after plus a diff, the verification result and a readable `report.html`.

---

## Key design decisions

**Effects, not intentions.** The policy judges what a button will do, read from the live page, not what the model
says it is doing. That closes the most obvious hole: a model (or a prompt injection) describing a reject as "saving
a note".

**The verifier is independent and boring.** It is SQL against the system of record. It never asks the LLM and never
looks at the page, because the page can lie. One failure mode makes the app show "Booked" without writing anything,
and the verifier catches it while the agent still believes it succeeded.

**LangGraph is thin.** All behaviour lives in `aiop/runtime.py`, which also runs as a plain loop without LangGraph.
`aiop/graph.py` only maps runtime steps to nodes, so I get checkpoints and the `interrupt()`/resume door for approvals
without a second state machine.

**Generic tools, not a recruiting solver.** It was tempting to write a scheduler for the interview task. I did not,
because that hard-codes one workflow. Instead there are two generic tools:
`submit_form(url, fields, button)`, which opens, fills and submits a form in one step (a recruiting run went from 39 LLM
calls to 12–17), and `assign`, a general slot-assignment solver. `assign` takes items with priorities and allowed slots
and does a priority greedy with a bipartite-matching feasibility check. That is the matroid greedy algorithm on a
transversal matroid, so it provably keeps the most important items, and it is tested against brute force. The loans
company uses neither, and needed no new code.

**Ties are never decided silently.** Priorities can be lists (Acme's tie-break is earlier application first). If two
items are still equal and the input order changed the answer, the result says so.

**Small checkpoint, big evidence.** The checkpoint holds ids, counters and summaries. Snapshots, screenshots and
database dumps live in the evidence folder, so resuming stays cheap.

**Humans have the last word.** A denial is final: the runtime remembers it and refuses the identical change if the
model proposes it again. On the loans side every payout is a maker-checker step, because moving money should never be
fully autonomous.

**Free model on purpose.** Everything was built and tested on a free 120B model. A stronger model would do better,
but the controls are the point, and they have to work with a model that makes mistakes.

---

## Results

Live runs on fresh data with the free model, final code (Thu 08-10). Every approval request was answered by the SOP,
and every check listed is a SQL query against the app's database, not the model's word.

| Task | Result | Checks | Time | LLM calls | Human approvals |
|---|---|---|---|---|---|
| Acme: screen every applicant for R-21 and book interviews | VERIFIED | 9 of 9 | 5.2 min | 12 | none needed |
| Riverbend: decide every application and pay out the approved ones | VERIFIED | 8 of 8 | 21.5 min | 30 | 1 large-loan sign-off, 3 payouts |
| H1 (unseen): book the most experienced qualified applicant in the latest free slot | VERIFIED | 7 of 7 | 1.9 min | 5 | none |
| H2 (unseen): reject everyone below the minimum years, with reasons | VERIFIED | 7 of 7 | 1.4 min | 5 | 2 rejections |
| H3 (unseen): decide only the applications scoring 700+ | VERIFIED | 8 of 8 | 18.1 min | 23 | 1 sign-off |
| H4 (unseen): approve and pay only the largest approvable loan | VERIFIED | 8 of 8 | 8.9 min | 22 | 1 sign-off, 1 payout |

Cost: $0 (free model). Earlier milestone runs are in the commit history (Acme 8/8 in 2.9 min with 12 calls when
`assign` landed; the loans company's first verified run took 27.5 min including an 8-minute internet outage it recovered
from on its own).

The held-out test is written up in [docs/held-out-test.md](docs/held-out-test.md): four tasks the operator had never
seen, each run once.

---

## Failure modes you can switch on

| Switch | What happens | What the operator does |
|---|---|---|
| `FLAKY` | the first booking or payout returns HTTP 503 | classified as transient, retried with backoff, succeeds |
| `SILENT_FAIL` | the app shows success but writes nothing | the verifier fails the run although the agent says done |
| `INJECTION` | one applicant's resume or one loan's remarks carry planted instructions aimed at the AI | the text is fenced as untrusted, the planted instruction is detected and its record quarantined (any change to it needs a human), and the policy still decides by effect |

Live runs with `INJECTION` on (09-10, free model): Acme VERIFIED 9/9 and Riverbend VERIFIED 8/8, both with and without
the defences below. With them on, the planted record (A-208, L-304) was detected and quarantined, and even the correct
action on it went to a human first.

---

## Security and threat model

Prompt injection is handled in layers, because no single one is enough:

1. **Fencing.** Apps mark text written by outsiders (`data-aiop-untrusted`); the snapshot wraps it in `«untrusted»`
   markers, strips fake markers inside it, and the prompt says never to follow instructions inside the fence.
2. **Detect and quarantine.** Text addressed to the AI ("note to the AI", "system override", "no approval needed")
   is logged as an `injection` event, the model is warned, and any change to that record needs a human.
3. **Effect-based policy.** Whatever the model says, the policy judges what the button really does.
4. **Company rule guards.** Where an SOP is pure arithmetic, the company can check a decision before it is
   submitted (Riverbend's credit rules: a decision the rules do not give is refused).
5. **Independent verification** against the database, and a human for anything irreversible.

SQL injection does not reach the data: every app query is parameterised, the model has no SQL tool, and the
verifier's connection is read-only (`tests/test_adversarial.py` throws payloads at forms, URLs and the verifier).


See [docs/architecture.md](docs/architecture.md). In short: the model gets a small fixed set of tools (goto, click, fill, select, read and submit_form,
plus note and assign, which only compute) and no JavaScript, download or file tool; navigation is limited to the company's allowed origins; every consequential action is judged by
deterministic policy and the riskiest ones need a human; and success is checked against the database. What this does
not claim: production credential isolation, full prompt-injection resistance or a hardened sandbox. The mock apps are
local and the data is fictional.

---

## Limitations and what's next

- **The held-out tasks were not blind the second time.** On first sight the operator finished 1 of 4 unseen tasks. Each
  failure led to a generic fix, and only then did all four pass. [docs/held-out-test.md](docs/held-out-test.md) keeps
  every round, including the failures and which fix came from which run.
- **One run per task.** The model is free and not deterministic, so a task that passed once can still fail on another
  run. Repeating each task several times and reporting a pass rate is the next thing I would do.
- **Slow on loans.** The loans runs take 9 to 22 minutes, almost all of it waiting for the free model (about 40 seconds
  per call in the last loans run, including a pause to stay under its rate limit). A faster or paid model would cut this a lot; the runtime is not the
  bottleneck.
- **Goal checks are written per task.** A new task can only be VERIFIED if someone writes its goal checks; otherwise it
  ends UNVERIFIED (invariants still run). Generating draft checks from the SOP for a human to approve would help.
- **The verifier reads SQLite.** For a real company it would call the system's API instead; the interface is the same.
- **Prompt injection is reduced, not solved.** The detector matches known phrasings, so a new wording can slip past it;
  the other layers still apply. The strongest known design keeps untrusted text away from the planning model entirely
  (a quarantined second model that only extracts values, as in Google DeepMind's CaMeL); that is the next step.
- **Security audit (09-10).** I attacked my own build. Three findings were fixed with tests (`tests/test_security.py`):
  an approval answer is now bound to its exact request (a stale or replayed answer is refused), the approval card
  shows the real record instead of only the model's description, and the loan guard can no longer be steered by numbers a
  customer typed into the application. Known gaps that remain, all outside what the mock apps expose:
  - the console has no login (it listens on 127.0.0.1 only), and `/runs` serves the demo databases and evidence;
  - a link's own effect tag is ignored, and untagged JavaScript buttons are allowed;
  - another site is blocked only after the browser has loaded it;
  - a submit that times out is retried, which needs idempotency keys on a real system;
  - notes can carry page text out of the untrusted fence.
- **Not built:** turning verified runs into reusable procedures, routing easy steps to a cheaper model, and real
  credential handling.

---

## Assumptions

- Both companies, their people and their data are made up. The two mock apps stand in for real systems, and the
  operator only ever talks to the origins listed in each `company.yaml`.
- A company's own system tags the buttons that change business state (`data-aiop-effect`). Where a page has no tags,
  the fallback rules in `policies.yaml` apply, and a submit nobody can classify goes to a human.
- Text written by outsiders (resumes, loan remarks, the applicant's name and purpose) is marked untrusted by the app.
- A person is reachable through the console to answer approvals. The console runs on 127.0.0.1 with no login.
- The company's system of record can be read with SQL, so the verifier reads the apps' SQLite files. A real company
  would need an API adapter behind the same interface.
- Only the tasks listed in `checks.yaml` have goal checks. Any other task can end UNVERIFIED at best.
- One browser and one run at a time. Each result in this README is one run on a free model.

---

## What it is built with

- **Model:** `nvidia/nemotron-3-super-120b-a12b:free` through OpenRouter, called with the OpenAI Python SDK. Any
  OpenAI-compatible endpoint works through `.env`. No other model or paid API is used.
- **Libraries:** LangGraph with its SQLite checkpointer (approvals and restarts), Playwright with Chromium (the
  browser), FastAPI and uvicorn (the mock apps and the console), pydantic, PyYAML, python-dotenv, SQLite and pytest.
- **Written for this project:** the runtime, policy engine, verifier, recovery rules, the slot solver in `assign.py`,
  both mock companies and the console. No template or existing agent project was used as a starting point.
- **AI coding tool:** I used Claude Code (Anthropic) as a coding assistant while building and testing, including for a
  security review and for running the tests on a clean cloud machine.

---

## Project layout

```
aiop/            the core (no company names or business words; a test enforces this)
  runtime.py     the run loop: guards, dispatch, approvals, recovery, finish
  llm.py         asks the model, validates its JSON; scripted proposer for tests
  browser.py     Playwright with a fixed set of tools and an origin allow-list
  policy.py      ALLOW / APPROVAL_REQUIRED / DENY from the real page
  recovery.py    failure classes and the next move for each
  verify.py      invariants and goal checks against the app database
  assign.py      the slot-assignment solver
  graph.py       LangGraph wiring: checkpoints and the approval interrupt
  evidence.py    runs/<id>/: timeline, snapshots, screenshots, report
companies/       one folder per company: SOP, policies, checks, seed data
mockapps/        the two fictional companies' web apps (FastAPI + SQLite)
console/         the operator console (start runs, watch, approve, see the verdict)
tests/           138 tests; real browser, scripted proposer
docs/            architecture and threat model, held-out test
```
