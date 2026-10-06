"""Mock recruiting company: a job board, an ATS and an interview calendar.

Three small "systems" served by one FastAPI app and backed by SQLite, so the
operator has real pages to click through and the verifier has a real database
to check. All data is fictional (companies/acme_talent/seed.json).

Run:  python -m mockapps.recruiting          (http://127.0.0.1:8001)
Set FLAKY=1 to make the first booking attempt fail, to exercise retries.
"""
import html
import json
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "companies" / "acme_talent" / "seed.json"
DB = ROOT / "runs" / "acme_talent.db"
STATUSES = ["Applied", "Shortlisted", "Not a fit (pending approval)", "Rejected", "Interview Scheduled"]

app = FastAPI(title="Acme Talent (mock)")
_flaky = {"left": 1 if os.getenv("FLAKY") == "1" else 0}


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def reset() -> None:
    DB.parent.mkdir(exist_ok=True)
    DB.unlink(missing_ok=True)
    seed = json.loads(SEED.read_text(encoding="utf-8"))
    con = db()
    con.executescript("""
        CREATE TABLE roles (id TEXT PRIMARY KEY, title TEXT, must_have TEXT, nice_to_have TEXT,
                            min_years INT, interviewer TEXT);
        CREATE TABLE candidates (id TEXT PRIMARY KEY, role TEXT, name TEXT, years INT, email TEXT,
                                 resume TEXT, status TEXT, note TEXT, slot TEXT);
        CREATE TABLE slots (id TEXT PRIMARY KEY, interviewer TEXT, start TEXT, status TEXT, candidate TEXT);
        CREATE TABLE audit (ts DATETIME DEFAULT CURRENT_TIMESTAMP, system TEXT, action TEXT);
    """)
    for r in seed["roles"]:
        con.execute("INSERT INTO roles VALUES (?,?,?,?,?,?)", (r["id"], r["title"], ", ".join(r["must_have"]),
                    ", ".join(r["nice_to_have"]), r["min_years"], r["interviewer"]))
    for a in seed["applicants"]:
        con.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
                    (a["id"], a["role"], a["name"], a["years"], a["email"], a["resume"], "Applied", "", None))
    for s in seed["calendar_slots"]:
        con.execute("INSERT INTO slots VALUES (?,?,?,?,?)", (s["id"], s["interviewer"], s["start"], s["status"], None))
    con.commit()
    con.close()


def page(title: str, body: str) -> HTMLResponse:
    nav = '<nav><a href="/">Home</a> · <a href="/jobs">Job board</a> · <a href="/ats">ATS</a> · <a href="/calendar">Calendar</a></nav>'
    return HTMLResponse(f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
                        "<style>body{font-family:sans-serif;max-width:900px;margin:20px auto}"
                        "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px}</style>"
                        f"</head><body>{nav}<h1>{html.escape(title)}</h1>{body}</body></html>")


def e(x) -> str:
    return html.escape(str(x if x is not None else ""))


@app.get("/")
def home():
    return page("Acme Talent intranet", "<p>Systems: <a href='/jobs'>Job board</a> (applications), "
                "<a href='/ats'>ATS</a> (candidate pipeline), <a href='/calendar'>Interview calendar</a>.</p>")


# ---------- Job board: read-only applications with resumes ----------
@app.get("/jobs")
def jobs():
    con = db()
    rows = con.execute("SELECT c.*, r.title FROM candidates c JOIN roles r ON r.id=c.role ORDER BY c.id").fetchall()
    con.close()
    trs = "".join(f"<tr><td>{e(r['id'])}</td><td>{e(r['name'])}</td><td>{e(r['title'])} ({e(r['role'])})</td>"
                  f"<td>{e(r['years'])}</td><td>{e(r['resume'])}</td></tr>" for r in rows)
    return page("Job board: applications", "<table><tr><th>Applicant</th><th>Name</th><th>Role</th>"
                f"<th>Years</th><th>Resume</th></tr>{trs}</table>")


# ---------- ATS: roles and candidate pipeline ----------
@app.get("/ats")
def ats():
    con = db()
    roles = con.execute("SELECT * FROM roles").fetchall()
    cands = con.execute("SELECT * FROM candidates ORDER BY id").fetchall()
    con.close()
    rl = "".join(f"<li><a href='/ats/role/{e(r['id'])}'>{e(r['id'])}: {e(r['title'])}</a></li>" for r in roles)
    trs = "".join(f"<tr><td><a href='/ats/candidate/{e(c['id'])}'>{e(c['id'])}</a></td><td>{e(c['name'])}</td>"
                  f"<td>{e(c['role'])}</td><td>{e(c['status'])}</td><td>{e(c['slot'])}</td></tr>" for c in cands)
    return page("ATS: pipeline", f"<h2>Roles</h2><ul>{rl}</ul><h2>Candidates</h2><table><tr><th>ID</th>"
                f"<th>Name</th><th>Role</th><th>Status</th><th>Interview slot</th></tr>{trs}</table>")


@app.get("/ats/role/{rid}")
def role(rid: str):
    con = db()
    r = con.execute("SELECT * FROM roles WHERE id=?", (rid,)).fetchone()
    con.close()
    if not r:
        return page("Not found", "<p>No such role.</p>")
    return page(f"Role {r['id']}: {r['title']}",
                f"<p><b>Must have:</b> {e(r['must_have'])}</p><p><b>Nice to have:</b> {e(r['nice_to_have'])}</p>"
                f"<p><b>Minimum years:</b> {e(r['min_years'])}</p><p><b>Interviewer:</b> {e(r['interviewer'])}</p>")


@app.get("/ats/candidate/{cid}")
def candidate(cid: str):
    con = db()
    c = con.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone()
    con.close()
    if not c:
        return page("Not found", "<p>No such candidate.</p>")
    opts = "".join(f"<option {'selected' if s == c['status'] else ''}>{e(s)}</option>" for s in STATUSES)
    form = (f"<form method='post' action='/ats/candidate/{e(cid)}'>"
            f"<label>Status <select name='status' id='status'>{opts}</select></label><br>"
            f"<label>Note <input name='note' id='note' size='60' value='{e(c['note'])}'></label><br>"
            "<button type='submit' id='save'>Save</button></form>")
    return page(f"Candidate {c['id']}: {c['name']}",
                f"<p>Role: {e(c['role'])} · Years: {e(c['years'])} · Status: <b id='current-status'>{e(c['status'])}</b>"
                f" · Slot: {e(c['slot'])}</p>{form}")


@app.post("/ats/candidate/{cid}")
def update_candidate(cid: str, status: str = Form(...), note: str = Form("")):
    if status not in STATUSES:
        return page("Error", "<p>Invalid status.</p>")
    con = db()
    con.execute("UPDATE candidates SET status=?, note=? WHERE id=?", (status, note, cid))
    con.execute("INSERT INTO audit(system, action) VALUES ('ats', ?)", (f"{cid} -> {status}",))
    con.commit()
    con.close()
    return RedirectResponse(f"/ats/candidate/{cid}?saved=1", status_code=303)


# ---------- Calendar: interviewer slots ----------
@app.get("/calendar")
def calendar():
    con = db()
    slots = con.execute("SELECT * FROM slots ORDER BY start").fetchall()
    con.close()
    trs = "".join(f"<tr><td>{e(s['id'])}</td><td>{e(s['interviewer'])}</td><td>{e(s['start'])}</td>"
                  f"<td>{e(s['status'])}</td><td>{e(s['candidate'])}</td></tr>" for s in slots)
    form = ("<h2>Book a slot</h2><form method='post' action='/calendar/book'>"
            "<label>Slot ID <input name='slot_id' id='slot_id'></label> "
            "<label>Candidate ID <input name='candidate_id' id='candidate_id'></label> "
            "<button type='submit' id='book'>Book</button></form>")
    return page("Interview calendar", f"<table><tr><th>Slot</th><th>Interviewer</th><th>Start</th>"
                f"<th>Status</th><th>Candidate</th></tr>{trs}</table>{form}")


@app.post("/calendar/book")
def book(slot_id: str = Form(...), candidate_id: str = Form(...)):
    if _flaky["left"] > 0:  # simulated outage for the reliability demo
        _flaky["left"] -= 1
        return page("Service unavailable", "<p id='error'>Calendar service temporarily unavailable. Try again.</p>")
    con = db()
    s = con.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
    c = con.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    if not s or not c:
        con.close()
        return page("Booking failed", "<p id='error'>Unknown slot or candidate.</p>")
    if s["status"] != "free":
        con.close()
        return page("Booking failed", f"<p id='error'>Slot {e(slot_id)} is not free.</p>")
    con.execute("UPDATE slots SET status='booked', candidate=? WHERE id=?", (candidate_id, slot_id))
    con.execute("UPDATE candidates SET status='Interview Scheduled', slot=? WHERE id=?", (slot_id, candidate_id))
    con.execute("INSERT INTO audit(system, action) VALUES ('calendar', ?)", (f"{slot_id} booked for {candidate_id}",))
    con.commit()
    con.close()
    return page("Booked", f"<p id='ok'>Slot {e(slot_id)} booked for {e(candidate_id)}.</p>")


# ---------- Machine endpoints (used by tests/reset only, never by the operator) ----------
@app.post("/admin/reset")
def admin_reset():
    reset()
    _flaky["left"] = 1 if os.getenv("FLAKY") == "1" else 0
    return JSONResponse({"ok": True})


if __name__ == "__main__":
    import uvicorn
    reset()
    uvicorn.run(app, host="127.0.0.1", port=8001)
