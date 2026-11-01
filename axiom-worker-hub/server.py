#!/usr/bin/env python3
"""Axiom Worker Hub - personal, local-first multi-AI coordination hub.

Zero third-party dependencies: Python 3.9+ standard library only.
All private data lives OUTSIDE the repo (default: ~/.axiom-worker-hub).

Usage:
  python3 server.py                 # localhost only
  python3 server.py --lan           # also reachable from phone on your Wi-Fi (token protected)
  python3 server.py backup [out.zip]
  python3 server.py restore backup.zip
"""
import argparse
import base64
import hmac
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP_DIR = Path(__file__).resolve().parent
STATIC = APP_DIR / "static"
DATA_DIR = Path(os.environ.get("AXIOM_HUB_DATA", str(Path.home() / ".axiom-worker-hub"))).expanduser()
DB_PATH = DATA_DIR / "hub.db"
ATT_DIR = DATA_DIR / "attachments"
BACKUP_DIR = DATA_DIR / "backups"
CONFIG_PATH = DATA_DIR / "config.json"
SEED_PATH = DATA_DIR / "seed.json"

MAX_UPLOAD = 10 * 1024 * 1024
TEXT_INLINE_LIMIT = 20 * 1024

# Provider homepages are well-known addresses but NOT verified live by this app.
# Override per worker with the "url" field in the Workers page.
PROVIDERS = {
    "claude": {"label": "Claude", "url": "https://claude.ai/",
               "roles": "Architecture, programming, implementation", "hint": ""},
    "chatgpt": {"label": "ChatGPT", "url": "https://chatgpt.com/",
                "roles": "Coordination, planning, quality assurance", "hint": ""},
    "deepseek": {"label": "DeepSeek", "url": "https://chat.deepseek.com/",
                 "roles": "Coding, debugging, technical problem-solving", "hint": ""},
    "kimi": {"label": "Kimi", "url": "https://www.kimi.com/",
             "roles": "Independent implementation, alternative approaches", "hint": ""},
    "perplexity": {"label": "Perplexity", "url": "https://www.perplexity.ai/",
                   "roles": "Web research, fact-checking, technical sources",
                   "hint": "Prioritise verifiable web sources and include full URLs for every factual claim."},
    "grok": {"label": "Grok", "url": "https://grok.com/",
             "roles": "Independent analysis, research, alternative perspectives", "hint": ""},
    "metaai": {"label": "Meta AI", "url": "https://www.meta.ai/",
               "roles": "General assistance, brainstorming, additional opinions", "hint": ""},
}

STATUSES = ["Backlog", "Assigned", "Working", "Submitted", "Review", "Owner Approval",
            "Completed", "Blocked", "Revisions Requested", "Rejected"]
PRIORITIES = ["Low", "Normal", "High", "Urgent"]
MEMORY_KINDS = ["research", "decision", "architecture", "document", "conversation", "issue", "completed", "note"]
AVAILABILITY = ["unknown", "available", "busy", "limit reached"]

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, instructions TEXT DEFAULT '', created TEXT);
CREATE TABLE IF NOT EXISTS memory(
  id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind TEXT DEFAULT 'note', title TEXT NOT NULL, body TEXT DEFAULT '', created TEXT);
CREATE TABLE IF NOT EXISTS workers(
  id INTEGER PRIMARY KEY, provider TEXT NOT NULL, name TEXT NOT NULL, roles TEXT DEFAULT '',
  capabilities TEXT DEFAULT '', instructions TEXT DEFAULT '', url TEXT DEFAULT '',
  active INTEGER DEFAULT 1, availability TEXT DEFAULT 'unknown', created TEXT);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  parent_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
  title TEXT NOT NULL, description TEXT DEFAULT '', priority TEXT DEFAULT 'Normal',
  status TEXT DEFAULT 'Backlog', decision TEXT DEFAULT '', decision_notes TEXT DEFAULT '',
  final_submission_id INTEGER, created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS assignments(
  id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  worker_id INTEGER NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
  kind TEXT DEFAULT 'work', review_of INTEGER, prompt TEXT DEFAULT '',
  status TEXT DEFAULT 'awaiting', created TEXT);
CREATE TABLE IF NOT EXISTS submissions(
  id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  worker_id INTEGER NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
  assignment_id INTEGER, kind TEXT DEFAULT 'work', review_of INTEGER,
  body TEXT DEFAULT '', pros TEXT DEFAULT '', cons TEXT DEFAULT '', sources TEXT DEFAULT '',
  conflicts TEXT DEFAULT '', missing TEXT DEFAULT '', created TEXT);
CREATE TABLE IF NOT EXISTS comments(
  id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  author TEXT DEFAULT 'Owner', body TEXT NOT NULL, created TEXT);
CREATE TABLE IF NOT EXISTS attachments(
  id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  filename TEXT NOT NULL, stored TEXT NOT NULL, size INTEGER, is_text INTEGER DEFAULT 0, created TEXT);
CREATE TABLE IF NOT EXISTS activity(
  id INTEGER PRIMARY KEY, ts TEXT, text TEXT, task_id INTEGER, project_id INTEGER);
"""


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def q(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def q1(db, sql, args=()):
    r = db.execute(sql, args).fetchone()
    return dict(r) if r else None


def log(db, text, task_id=None, project_id=None):
    db.execute("INSERT INTO activity(ts,text,task_id,project_id) VALUES(?,?,?,?)",
               (now(), text, task_id, project_id))


def init_data():
    for d in (DATA_DIR, ATT_DIR, BACKUP_DIR):
        d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass
    db = connect()
    db.executescript(SCHEMA)
    db.execute("PRAGMA journal_mode=WAL")
    if db.execute("SELECT COUNT(*) FROM workers").fetchone()[0] == 0:
        for key, p in PROVIDERS.items():
            db.execute("INSERT INTO workers(provider,name,roles,capabilities,created) VALUES(?,?,?,?,?)",
                       (key, f"{p['label']} #1", p["roles"], "", now()))
    if db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0:
        if SEED_PATH.exists():
            try:
                seed = json.loads(SEED_PATH.read_text())
                for pr in seed.get("projects", []):
                    cur = db.execute("INSERT INTO projects(name,instructions,created) VALUES(?,?,?)",
                                     (pr.get("name", "Project"), pr.get("instructions", ""), now()))
                    for m in pr.get("memory", []):
                        db.execute("INSERT INTO memory(project_id,kind,title,body,created) VALUES(?,?,?,?,?)",
                                   (cur.lastrowid, m.get("kind", "note"), m.get("title", "Note"), m.get("body", ""), now()))
            except Exception as e:  # bad seed must not stop the app
                print("seed.json ignored:", e)
        if db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0:
            db.execute("INSERT INTO projects(name,instructions,created) VALUES(?,?,?)",
                       ("My first project", "", now()))
    db.commit()
    db.close()
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps({"lan_token": secrets.token_urlsafe(24)}))
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass


def lan_token():
    return json.loads(CONFIG_PATH.read_text())["lan_token"]


# ---------------------------------------------------------------- prompts

def parse_sections(body):
    out, cur = {}, None
    for line in body.splitlines():
        m = re.match(r"^\s*(?:#{1,4}\s*|\*\*)\s*([A-Za-z &/\-]+?)\s*(?:\*\*)?\s*:?\s*$", line)
        if m:
            cur = m.group(1).lower()
            out[cur] = []
        elif cur:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def pick(sections, any_of, none_of=()):
    for k, v in sections.items():
        if any(a in k for a in any_of) and not any(n in k for n in none_of) and v:
            return v
    return ""


def keywords(text):
    return {w for w in re.findall(r"[a-z0-9]{4,}", text.lower())}


def suggest_memory(db, task):
    kw = keywords(task["title"] + " " + task["description"])
    scored = []
    for m in q(db, "SELECT * FROM memory WHERE project_id=?", (task["project_id"],)):
        score = len(kw & keywords(m["title"] + " " + m["body"]))
        if score:
            scored.append((score, m["id"]))
    scored.sort(reverse=True)
    return [i for _, i in scored[:5]]


RESPONSE_FORMAT = """## How to respond
Work independently. Do not assume other assistants are correct. Structure your answer with these headings:
## Solution
## Advantages
## Disadvantages
## Assumptions and missing evidence
## Sources
State uncertainty plainly. Do not invent sources; if you cannot verify something, say so."""


def attachments_block(db, task_id):
    rows = q(db, "SELECT * FROM attachments WHERE task_id=?", (task_id,))
    if not rows:
        return ""
    parts = ["## Attachments"]
    for a in rows:
        if a["is_text"] and a["size"] <= TEXT_INLINE_LIMIT:
            txt = (ATT_DIR / a["stored"]).read_text(errors="replace")
            parts.append(f"### {a['filename']}\n```\n{txt}\n```")
        else:
            parts.append(f"- {a['filename']} (file not included in this prompt - upload it manually in the AI app if needed)")
    return "\n".join(parts)


def build_prompt(db, task, worker, kind, memory_ids, review_sub=None, extra=""):
    project = q1(db, "SELECT * FROM projects WHERE id=?", (task["project_id"],))
    prov = PROVIDERS.get(worker["provider"], {})
    role = worker["roles"] or prov.get("roles", "")
    out = [f'You are acting as "{worker["name"]}" ({prov.get("label", worker["provider"])}) '
           f"in a multi-AI workflow coordinated by the project owner."
           + (f" Your role: {role}." if role else "")]
    if project["instructions"].strip():
        out.append("## Project instructions\n" + project["instructions"].strip())
    out.append(f"## Task: {task['title']}  (priority: {task['priority']})\n{task['description'].strip()}")
    parent = q1(db, "SELECT title FROM tasks WHERE id=?", (task["parent_id"],)) if task["parent_id"] else None
    if parent:
        out.append(f"(This is a subtask of: {parent['title']})")
    mems = []
    for mid in memory_ids:
        m = q1(db, "SELECT * FROM memory WHERE id=? AND project_id=?", (mid, task["project_id"]))
        if m:
            mems.append(f"### {m['title']} [{m['kind']}]\n{m['body'].strip()}")
    if mems:
        out.append("## Relevant context\n" + "\n\n".join(mems))
    att = attachments_block(db, task["id"])
    if att:
        out.append(att)
    wi = "\n".join(x for x in (worker["instructions"].strip(), prov.get("hint", "")) if x)
    if wi:
        out.append("## Worker-specific instructions\n" + wi)
    if extra.strip():
        out.append("## Additional notes from the owner\n" + extra.strip())

    if kind == "review" and review_sub:
        out.append("## Submission under review\n" + review_sub["body"].strip())
        out.append("""## How to respond
You are an independent reviewer. Do not assume the submission is correct. Use these headings:
## Verdict (Accept / Revise / Reject)
## Errors and risks
## Missing evidence
## Unsupported claims
## Suggested improvements
## Sources
Verify claims where you can and say what you could not verify.""")
    elif kind == "consolidate":
        subs = q(db, "SELECT s.*, w.name wname FROM submissions s JOIN workers w ON w.id=s.worker_id "
                     "WHERE s.task_id=? AND s.kind IN ('work') ORDER BY s.id", (task["id"],))
        revs = q(db, "SELECT s.*, w.name wname FROM submissions s JOIN workers w ON w.id=s.worker_id "
                     "WHERE s.task_id=? AND s.kind='review' ORDER BY s.id", (task["id"],))
        block = ["## Submissions from independent workers"]
        for s in subs:
            block.append(f"### Submission #{s['id']} by {s['wname']}\n{s['body'].strip()}")
            for r in [r for r in revs if r["review_of"] == s["id"]]:
                block.append(f"#### Review of #{s['id']} by {r['wname']}\n{r['body'].strip()}")
        out.append("\n\n".join(block))
        out.append("""## How to respond
Produce ONE consolidated solution. Use these headings:
## Consolidated solution
## What was taken from each submission
## Conflicts between submissions and how you resolved them
## Remaining uncertainty and missing evidence
## Sources
Do not assume any submission is correct; flag anything unverified.""")
    else:
        out.append(RESPONSE_FORMAT)
    return "\n\n".join(out)


# ---------------------------------------------------------------- api

class ApiError(Exception):
    def __init__(self, msg, status=400):
        self.msg, self.status = msg, status


def need(body, *keys):
    for k in keys:
        if not str(body.get(k, "")).strip():
            raise ApiError(f"'{k}' is required")


ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


def get_task(db, tid):
    t = q1(db, "SELECT * FROM tasks WHERE id=?", (tid,))
    if not t:
        raise ApiError("task not found", 404)
    return t


def touch(db, tid):
    db.execute("UPDATE tasks SET updated=? WHERE id=?", (now(), tid))


@route("GET", r"/api/meta")
def meta(db, m, body, qs):
    return {"providers": PROVIDERS, "statuses": STATUSES, "priorities": PRIORITIES,
            "memory_kinds": MEMORY_KINDS, "availability": AVAILABILITY, "data_dir": str(DATA_DIR)}


# projects
@route("GET", r"/api/projects")
def projects_list(db, m, body, qs):
    return q(db, "SELECT p.*, (SELECT COUNT(*) FROM tasks t WHERE t.project_id=p.id) task_count, "
                 "(SELECT COUNT(*) FROM memory x WHERE x.project_id=p.id) memory_count "
                 "FROM projects p ORDER BY p.id")


@route("POST", r"/api/projects")
def projects_create(db, m, body, qs):
    need(body, "name")
    cur = db.execute("INSERT INTO projects(name,instructions,created) VALUES(?,?,?)",
                     (body["name"].strip(), body.get("instructions", ""), now()))
    log(db, f"Project created: {body['name'].strip()}", project_id=cur.lastrowid)
    return {"id": cur.lastrowid}


@route("PUT", r"/api/projects/(\d+)")
def projects_update(db, m, body, qs):
    pid = int(m[1])
    need(body, "name")
    db.execute("UPDATE projects SET name=?, instructions=? WHERE id=?",
               (body["name"].strip(), body.get("instructions", ""), pid))
    log(db, f"Project updated: {body['name'].strip()}", project_id=pid)
    return {"ok": True}


@route("DELETE", r"/api/projects/(\d+)")
def projects_delete(db, m, body, qs):
    if qs.get("confirm") != "yes":
        raise ApiError("Owner confirmation required (confirm=yes)", 403)
    pid = int(m[1])
    for a in q(db, "SELECT a.stored FROM attachments a JOIN tasks t ON t.id=a.task_id WHERE t.project_id=?", (pid,)):
        (ATT_DIR / a["stored"]).unlink(missing_ok=True)
    db.execute("DELETE FROM projects WHERE id=?", (pid,))
    log(db, f"Project #{pid} deleted (owner confirmed)")
    return {"ok": True}


# memory
@route("GET", r"/api/projects/(\d+)/memory")
def memory_list(db, m, body, qs):
    return q(db, "SELECT * FROM memory WHERE project_id=? ORDER BY id DESC", (int(m[1]),))


@route("POST", r"/api/projects/(\d+)/memory")
def memory_create(db, m, body, qs):
    need(body, "title")
    kind = body.get("kind", "note")
    cur = db.execute("INSERT INTO memory(project_id,kind,title,body,created) VALUES(?,?,?,?,?)",
                     (int(m[1]), kind if kind in MEMORY_KINDS else "note", body["title"].strip(), body.get("body", ""), now()))
    log(db, f"Memory added: {body['title'].strip()}", project_id=int(m[1]))
    return {"id": cur.lastrowid}


@route("PUT", r"/api/memory/(\d+)")
def memory_update(db, m, body, qs):
    need(body, "title")
    kind = body.get("kind", "note")
    db.execute("UPDATE memory SET kind=?, title=?, body=? WHERE id=?",
               (kind if kind in MEMORY_KINDS else "note", body["title"].strip(), body.get("body", ""), int(m[1])))
    return {"ok": True}


@route("DELETE", r"/api/memory/(\d+)")
def memory_delete(db, m, body, qs):
    if qs.get("confirm") != "yes":
        raise ApiError("Owner confirmation required (confirm=yes)", 403)
    db.execute("DELETE FROM memory WHERE id=?", (int(m[1]),))
    return {"ok": True}


# workers
@route("GET", r"/api/workers")
def workers_list(db, m, body, qs):
    return q(db, "SELECT w.*, (SELECT COUNT(*) FROM assignments a WHERE a.worker_id=w.id) assignment_count "
                 "FROM workers w ORDER BY w.provider, w.id")


@route("POST", r"/api/workers")
def workers_create(db, m, body, qs):
    need(body, "provider", "name")
    if body["provider"] not in PROVIDERS:
        raise ApiError("unknown provider")
    cur = db.execute("INSERT INTO workers(provider,name,roles,capabilities,instructions,url,created) VALUES(?,?,?,?,?,?,?)",
                     (body["provider"], body["name"].strip(), body.get("roles", ""), body.get("capabilities", ""),
                      body.get("instructions", ""), body.get("url", ""), now()))
    log(db, f"Worker registered: {body['name'].strip()}")
    return {"id": cur.lastrowid}


@route("PUT", r"/api/workers/(\d+)")
def workers_update(db, m, body, qs):
    wid = int(m[1])
    w = q1(db, "SELECT * FROM workers WHERE id=?", (wid,))
    if not w:
        raise ApiError("worker not found", 404)
    av = body.get("availability", w["availability"])
    if av not in AVAILABILITY:
        av = "unknown"
    db.execute("UPDATE workers SET name=?, roles=?, capabilities=?, instructions=?, url=?, active=?, availability=? WHERE id=?",
               (body.get("name", w["name"]).strip() or w["name"], body.get("roles", w["roles"]),
                body.get("capabilities", w["capabilities"]), body.get("instructions", w["instructions"]),
                body.get("url", w["url"]), 1 if body.get("active", w["active"]) else 0, av, wid))
    return {"ok": True}


@route("DELETE", r"/api/workers/(\d+)")
def workers_delete(db, m, body, qs):
    if qs.get("confirm") != "yes":
        raise ApiError("Owner confirmation required (confirm=yes)", 403)
    db.execute("DELETE FROM workers WHERE id=?", (int(m[1]),))
    log(db, f"Worker #{m[1]} deleted (owner confirmed)")
    return {"ok": True}


@route("GET", r"/api/workers/(\d+)/history")
def workers_history(db, m, body, qs):
    wid = int(m[1])
    return {"assignments": q(db, "SELECT a.*, t.title task_title FROM assignments a JOIN tasks t ON t.id=a.task_id "
                                 "WHERE a.worker_id=? ORDER BY a.id DESC LIMIT 50", (wid,)),
            "submissions": q(db, "SELECT s.id, s.task_id, s.kind, s.created, substr(s.body,1,300) excerpt, t.title task_title "
                                 "FROM submissions s JOIN tasks t ON t.id=s.task_id WHERE s.worker_id=? ORDER BY s.id DESC LIMIT 50", (wid,))}


# tasks
@route("GET", r"/api/tasks")
def tasks_list(db, m, body, qs):
    sql = ("SELECT t.*, p.name project_name, "
           "(SELECT COUNT(*) FROM assignments a WHERE a.task_id=t.id AND a.status IN ('awaiting','sent')) open_assignments, "
           "(SELECT COUNT(*) FROM submissions s WHERE s.task_id=t.id AND s.kind='work') submissions, "
           "(SELECT COUNT(*) FROM tasks c WHERE c.parent_id=t.id) subtasks "
           "FROM tasks t JOIN projects p ON p.id=t.project_id")
    args = []
    if qs.get("project_id"):
        sql += " WHERE t.project_id=?"
        args.append(int(qs["project_id"]))
    sql += " ORDER BY t.id DESC"
    return q(db, sql, args)


@route("POST", r"/api/tasks")
def tasks_create(db, m, body, qs):
    need(body, "title", "project_id")
    pri = body.get("priority", "Normal")
    cur = db.execute("INSERT INTO tasks(project_id,parent_id,title,description,priority,status,created,updated) "
                     "VALUES(?,?,?,?,?,'Backlog',?,?)",
                     (int(body["project_id"]), body.get("parent_id") or None, body["title"].strip(),
                      body.get("description", ""), pri if pri in PRIORITIES else "Normal", now(), now()))
    log(db, f"Task created: {body['title'].strip()}", cur.lastrowid, int(body["project_id"]))
    return {"id": cur.lastrowid}


@route("GET", r"/api/tasks/(\d+)")
def tasks_get(db, m, body, qs):
    t = get_task(db, int(m[1]))
    tid = t["id"]
    t["project"] = q1(db, "SELECT * FROM projects WHERE id=?", (t["project_id"],))
    t["parent"] = q1(db, "SELECT id,title FROM tasks WHERE id=?", (t["parent_id"],)) if t["parent_id"] else None
    t["subtasks"] = q(db, "SELECT id,title,status,priority FROM tasks WHERE parent_id=? ORDER BY id", (tid,))
    t["assignments"] = q(db, "SELECT a.*, w.name worker_name, w.provider, w.url worker_url FROM assignments a "
                             "JOIN workers w ON w.id=a.worker_id WHERE a.task_id=? ORDER BY a.id DESC", (tid,))
    t["submissions"] = q(db, "SELECT s.*, w.name worker_name, w.provider FROM submissions s "
                             "JOIN workers w ON w.id=s.worker_id WHERE s.task_id=? ORDER BY s.id", (tid,))
    t["comments"] = q(db, "SELECT * FROM comments WHERE task_id=? ORDER BY id", (tid,))
    t["attachments"] = q(db, "SELECT id,filename,size,is_text,created FROM attachments WHERE task_id=?", (tid,))
    t["activity"] = q(db, "SELECT * FROM activity WHERE task_id=? ORDER BY id DESC LIMIT 30", (tid,))
    t["suggested_memory"] = suggest_memory(db, t)
    t["memory"] = q(db, "SELECT id,kind,title FROM memory WHERE project_id=? ORDER BY id DESC", (t["project_id"],))
    return t


@route("PUT", r"/api/tasks/(\d+)")
def tasks_update(db, m, body, qs):
    t = get_task(db, int(m[1]))
    status = body.get("status", t["status"])
    if status not in STATUSES:
        raise ApiError("invalid status")
    if status == "Completed" and t["status"] != "Completed":
        raise ApiError("A task can only be Completed through the owner decision (Approve).", 403)
    pri = body.get("priority", t["priority"])
    db.execute("UPDATE tasks SET title=?, description=?, priority=?, status=?, updated=? WHERE id=?",
               (body.get("title", t["title"]).strip() or t["title"], body.get("description", t["description"]),
                pri if pri in PRIORITIES else t["priority"], status, now(), t["id"]))
    if status != t["status"]:
        log(db, f"Task '{t['title']}': {t['status']} -> {status}", t["id"], t["project_id"])
    return {"ok": True}


@route("DELETE", r"/api/tasks/(\d+)")
def tasks_delete(db, m, body, qs):
    if qs.get("confirm") != "yes":
        raise ApiError("Owner confirmation required (confirm=yes)", 403)
    t = get_task(db, int(m[1]))
    for a in q(db, "SELECT stored FROM attachments WHERE task_id=?", (t["id"],)):
        (ATT_DIR / a["stored"]).unlink(missing_ok=True)
    db.execute("DELETE FROM tasks WHERE id=?", (t["id"],))
    log(db, f"Task deleted (owner confirmed): {t['title']}", project_id=t["project_id"])
    return {"ok": True}


@route("POST", r"/api/tasks/(\d+)/assign")
def tasks_assign(db, m, body, qs):
    t = get_task(db, int(m[1]))
    kind = body.get("kind", "work")
    if kind not in ("work", "review", "consolidate"):
        raise ApiError("invalid kind")
    ids = [int(x) for x in body.get("worker_ids", [])]
    if not ids:
        raise ApiError("select at least one worker")
    review_sub = None
    if kind == "review":
        review_sub = q1(db, "SELECT * FROM submissions WHERE id=? AND task_id=?", (body.get("review_of"), t["id"]))
        if not review_sub:
            raise ApiError("choose a submission to review")
    if kind == "consolidate" and not q(db, "SELECT 1 FROM submissions WHERE task_id=? AND kind='work'", (t["id"],)):
        raise ApiError("no submissions to consolidate yet")
    mem_ids = body.get("memory_ids")
    if mem_ids is None:
        mem_ids = suggest_memory(db, t)
    created = []
    for wid in ids:
        w = q1(db, "SELECT * FROM workers WHERE id=?", (wid,))
        if not w:
            raise ApiError(f"worker {wid} not found", 404)
        prompt = build_prompt(db, t, w, kind, [int(x) for x in mem_ids], review_sub, body.get("extra", ""))
        cur = db.execute("INSERT INTO assignments(task_id,worker_id,kind,review_of,prompt,status,created) VALUES(?,?,?,?,?,'awaiting',?)",
                         (t["id"], wid, kind, review_sub["id"] if review_sub else None, prompt, now()))
        created.append(cur.lastrowid)
        log(db, f"{kind.capitalize()} prompt generated for {w['name']} on '{t['title']}'", t["id"], t["project_id"])
    if kind == "work" and t["status"] in ("Backlog", "Revisions Requested", "Rejected"):
        db.execute("UPDATE tasks SET status='Assigned' WHERE id=?", (t["id"],))
    if kind == "review":
        db.execute("UPDATE tasks SET status='Review' WHERE id=? AND status IN ('Submitted','Assigned','Working','Review')", (t["id"],))
    touch(db, t["id"])
    return {"assignment_ids": created}


@route("POST", r"/api/tasks/(\d+)/comments")
def comments_create(db, m, body, qs):
    t = get_task(db, int(m[1]))
    need(body, "body")
    db.execute("INSERT INTO comments(task_id,author,body,created) VALUES(?,?,?,?)",
               (t["id"], body.get("author", "Owner") or "Owner", body["body"], now()))
    log(db, f"Comment on '{t['title']}'", t["id"], t["project_id"])
    return {"ok": True}


@route("POST", r"/api/tasks/(\d+)/attachments")
def attachments_create(db, m, body, qs):
    t = get_task(db, int(m[1]))
    need(body, "filename", "data")
    try:
        raw = base64.b64decode(body["data"], validate=False)
    except Exception:
        raise ApiError("bad file data")
    if len(raw) > MAX_UPLOAD:
        raise ApiError("file too large (10 MB max)")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(body["filename"]).name)[:80] or "file"
    stored = f"{secrets.token_hex(6)}_{safe}"
    (ATT_DIR / stored).write_bytes(raw)
    try:
        raw.decode("utf-8")
        is_text = 1
    except UnicodeDecodeError:
        is_text = 0
    db.execute("INSERT INTO attachments(task_id,filename,stored,size,is_text,created) VALUES(?,?,?,?,?,?)",
               (t["id"], Path(body["filename"]).name, stored, len(raw), is_text, now()))
    log(db, f"Attachment added to '{t['title']}': {safe}", t["id"], t["project_id"])
    return {"ok": True}


@route("DELETE", r"/api/attachments/(\d+)")
def attachments_delete(db, m, body, qs):
    if qs.get("confirm") != "yes":
        raise ApiError("Owner confirmation required (confirm=yes)", 403)
    a = q1(db, "SELECT * FROM attachments WHERE id=?", (int(m[1]),))
    if a:
        (ATT_DIR / a["stored"]).unlink(missing_ok=True)
        db.execute("DELETE FROM attachments WHERE id=?", (a["id"],))
    return {"ok": True}


@route("POST", r"/api/tasks/(\d+)/decision")
def tasks_decision(db, m, body, qs):
    t = get_task(db, int(m[1]))
    mapping = {"approve": "Completed", "revise": "Revisions Requested", "reject": "Rejected"}
    d = body.get("decision")
    if d not in mapping:
        raise ApiError("decision must be approve, revise or reject")
    db.execute("UPDATE tasks SET status=?, decision=?, decision_notes=?, final_submission_id=?, updated=? WHERE id=?",
               (mapping[d], d, body.get("notes", ""), body.get("final_submission_id") or None, now(), t["id"]))
    log(db, f"Owner decision on '{t['title']}': {d}", t["id"], t["project_id"])
    return {"status": mapping[d]}


# assignments / submissions
@route("PUT", r"/api/assignments/(\d+)")
def assignments_update(db, m, body, qs):
    a = q1(db, "SELECT * FROM assignments WHERE id=?", (int(m[1]),))
    if not a:
        raise ApiError("assignment not found", 404)
    st = body.get("status")
    if st not in ("awaiting", "sent", "cancelled"):
        raise ApiError("invalid status")
    db.execute("UPDATE assignments SET status=? WHERE id=?", (st, a["id"]))
    if st == "sent":
        db.execute("UPDATE tasks SET status='Working' WHERE id=? AND status IN ('Backlog','Assigned','Revisions Requested')", (a["task_id"],))
    touch(db, a["task_id"])
    return {"ok": True}


@route("POST", r"/api/assignments/(\d+)/submit")
def assignments_submit(db, m, body, qs):
    a = q1(db, "SELECT * FROM assignments WHERE id=?", (int(m[1]),))
    if not a:
        raise ApiError("assignment not found", 404)
    if a["status"] == "submitted":
        raise ApiError("already submitted", 409)
    need(body, "body")
    text = body["body"]
    sec = parse_sections(text)
    pros = body.get("pros") or pick(sec, ("advantage", "pros", "strength"), ("disadvantage",))
    cons = body.get("cons") or pick(sec, ("disadvantage", "cons", "weakness", "risk"))
    sources = body.get("sources") or pick(sec, ("source", "reference"))
    missing = body.get("missing") or pick(sec, ("missing", "assumption"))
    cur = db.execute("INSERT INTO submissions(task_id,worker_id,assignment_id,kind,review_of,body,pros,cons,sources,missing,created) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (a["task_id"], a["worker_id"], a["id"], a["kind"], a["review_of"], text, pros, cons, sources, missing, now()))
    db.execute("UPDATE assignments SET status='submitted' WHERE id=?", (a["id"],))
    t = get_task(db, a["task_id"])
    if a["kind"] == "work":
        pending = db.execute("SELECT COUNT(*) FROM assignments WHERE task_id=? AND kind='work' AND status IN ('awaiting','sent')",
                             (t["id"],)).fetchone()[0]
        if t["status"] in ("Backlog", "Assigned", "Working", "Revisions Requested"):
            db.execute("UPDATE tasks SET status=? WHERE id=?", ("Submitted" if pending == 0 else "Working", t["id"]))
    elif a["kind"] == "consolidate":
        db.execute("UPDATE tasks SET status='Owner Approval' WHERE id=?", (t["id"],))
    touch(db, t["id"])
    w = q1(db, "SELECT name FROM workers WHERE id=?", (a["worker_id"],))
    log(db, f"{a['kind'].capitalize()} response saved from {w['name']} on '{t['title']}'", t["id"], t["project_id"])
    return {"submission_id": cur.lastrowid}


@route("PUT", r"/api/submissions/(\d+)")
def submissions_update(db, m, body, qs):
    s = q1(db, "SELECT * FROM submissions WHERE id=?", (int(m[1]),))
    if not s:
        raise ApiError("submission not found", 404)
    vals = {k: body.get(k, s[k]) for k in ("pros", "cons", "sources", "conflicts", "missing")}
    db.execute("UPDATE submissions SET pros=?, cons=?, sources=?, conflicts=?, missing=? WHERE id=?",
               (vals["pros"], vals["cons"], vals["sources"], vals["conflicts"], vals["missing"], s["id"]))
    return {"ok": True}


@route("GET", r"/api/attachments/(\d+)")
def attachments_get(db, m, body, qs):
    a = q1(db, "SELECT * FROM attachments WHERE id=?", (int(m[1]),))
    if not a:
        raise ApiError("not found", 404)
    return ("__file__", ATT_DIR / a["stored"], a["filename"])


# dashboard
@route("GET", r"/api/dashboard")
def dashboard(db, m, body, qs):
    pid = qs.get("project_id")
    tw, args = (" WHERE project_id=?", [int(pid)]) if pid else ("", [])
    by = {r["status"]: r["n"] for r in q(db, f"SELECT status, COUNT(*) n FROM tasks{tw} GROUP BY status", args)}
    aj = "FROM assignments a JOIN tasks t ON t.id=a.task_id JOIN workers w ON w.id=a.worker_id " \
         "WHERE a.status IN ('awaiting','sent')" + (" AND t.project_id=?" if pid else "")
    open_a = q(db, "SELECT a.id, a.kind, a.status, a.task_id, t.title task_title, w.name worker_name, w.provider " + aj + " ORDER BY a.id DESC", args)
    approvals = q(db, "SELECT id,title,priority FROM tasks WHERE status='Owner Approval'" + (" AND project_id=?" if pid else ""), args)
    blocked = q(db, "SELECT id,title,priority FROM tasks WHERE status='Blocked'" + (" AND project_id=?" if pid else ""), args)
    act_sql = "SELECT * FROM activity" + (" WHERE project_id=?" if pid else "") + " ORDER BY id DESC LIMIT 15"
    return {
        "workers_total": db.execute("SELECT COUNT(*) FROM workers").fetchone()[0],
        "workers_active": db.execute("SELECT COUNT(*) FROM workers WHERE active=1").fetchone()[0],
        "by_status": by,
        "pending_submissions": sum(1 for a in open_a if a["kind"] in ("work", "consolidate")),
        "pending_reviews": sum(1 for a in open_a if a["kind"] == "review"),
        "open_assignments": open_a, "approvals": approvals, "blocked": blocked,
        "activity": q(db, act_sql, args),
    }


# backup
def make_backup_zip(dest):
    tmp = Path(tempfile.mkdtemp())
    try:
        snap = tmp / "hub.db"
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(snap)
        src.backup(dst)
        dst.close()
        src.close()
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "hub.db")
            if CONFIG_PATH.exists():
                z.write(CONFIG_PATH, "config.json")
            for f in ATT_DIR.glob("*"):
                z.write(f, f"attachments/{f.name}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@route("POST", r"/api/backup")
def backup_save(db, m, body, qs):
    dest = BACKUP_DIR / f"hub-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    make_backup_zip(dest)
    log(db, f"Backup saved: {dest.name}")
    return {"path": str(dest)}


@route("GET", r"/api/backups")
def backups_list(db, m, body, qs):
    return [{"name": f.name, "size": f.stat().st_size} for f in sorted(BACKUP_DIR.glob("*.zip"), reverse=True)]


@route("GET", r"/api/backup")
def backup_download(db, m, body, qs):
    tmp = Path(tempfile.mkdtemp()) / "backup.zip"
    make_backup_zip(tmp)
    return ("__file__", tmp, f"axiom-hub-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip")


def restore_backup(zip_path):
    zip_path = Path(zip_path)
    if not zip_path.exists():
        sys.exit(f"Backup not found: {zip_path}")
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if "hub.db" not in names:
            sys.exit("Not a valid hub backup (hub.db missing).")
        for n in names:
            if n.startswith("/") or ".." in Path(n).parts:
                sys.exit("Unsafe path in backup.")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if DB_PATH.exists():
            safety = DATA_DIR / f"pre-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
            make_backup_zip(safety)
            print("Safety copy of current data:", safety)
        for ext in ("-wal", "-shm"):
            Path(str(DB_PATH) + ext).unlink(missing_ok=True)
        ATT_DIR.mkdir(exist_ok=True)
        for f in ATT_DIR.glob("*"):
            f.unlink()
        for n in names:
            if n == "hub.db":
                DB_PATH.write_bytes(z.read(n))
            elif n == "config.json":
                CONFIG_PATH.write_bytes(z.read(n))
            elif n.startswith("attachments/") and not n.endswith("/"):
                (ATT_DIR / Path(n).name).write_bytes(z.read(n))
    print("Restore complete. Start the hub again.")


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "AxiomHub/1.0"
    lan = False

    def log_message(self, fmt, *args):  # keep terminal quiet / avoid logging private content
        pass

    def _client_is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if self.lan:
            return True
        return host in ("localhost", "127.0.0.1", "::1")

    def _authorized(self, url):
        if self._client_is_local():
            return True, None
        token = lan_token()
        supplied = parse_qs(url.query).get("token", [None])[0]
        cookie = ""
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "hub_token":
                cookie = v
        for cand in (supplied, cookie, self.headers.get("X-Hub-Token")):
            if cand and hmac.compare_digest(cand, token):
                return True, (token if cand is supplied else None)
        return False, None

    def _send(self, status, data, ctype="application/json", extra=None):
        if not isinstance(data, (bytes, bytearray)):
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        url = urlparse(self.path)
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        ok, set_cookie = self._authorized(url)
        if not ok:
            return self._send(401, b"Unauthorized: open the full URL printed in the terminal (it includes ?token=...).", "text/plain")
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            return self._send(403, {"error": "cross-origin request blocked"})
        extra = {"Set-Cookie": f"hub_token={set_cookie}; Path=/; HttpOnly; SameSite=Strict"} if set_cookie else None

        if not url.path.startswith("/api/"):
            if method != "GET":
                return self._send(405, {"error": "method not allowed"})
            rel = "index.html" if url.path in ("/", "") else url.path.lstrip("/")
            f = (STATIC / rel).resolve()
            if STATIC.resolve() not in f.parents or not f.is_file():
                return self._send(404, b"Not found", "text/plain")
            ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css"}.get(f.suffix, "application/octet-stream")
            return self._send(200, f.read_bytes(), ctype, extra)

        body = {}
        if method in ("POST", "PUT"):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_UPLOAD * 2:
                return self._send(413, {"error": "too large"})
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._send(415, {"error": "JSON required"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid JSON"})
        qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        for meth, rx, fn in ROUTES:
            mt = rx.match(url.path)
            if meth == method and mt:
                db = connect()
                try:
                    res = fn(db, mt, body, qs)
                    db.commit()
                except ApiError as e:
                    db.rollback()
                    return self._send(e.status, {"error": e.msg}, extra=extra)
                except Exception as e:
                    db.rollback()
                    print("error:", type(e).__name__, e)
                    return self._send(500, {"error": "internal error"}, extra=extra)
                finally:
                    db.close()
                if isinstance(res, tuple) and res and res[0] == "__file__":
                    _, path, name = res
                    data = Path(path).read_bytes()
                    return self._send(200, data, "application/octet-stream",
                                      {"Content-Disposition": f'attachment; filename="{name}"'})
                return self._send(200, res, extra=extra)
        self._send(404, {"error": "not found"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is actually sent
        return s.getsockname()[0]
    except OSError:
        return "YOUR-COMPUTER-IP"
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(description="Axiom Worker Hub")
    ap.add_argument("command", nargs="?", default="serve", choices=["serve", "backup", "restore"])
    ap.add_argument("path", nargs="?")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--lan", action="store_true", help="allow phone access on your local network (token protected)")
    a = ap.parse_args()

    if a.command == "restore":
        if not a.path:
            sys.exit("usage: python3 server.py restore backup.zip")
        return restore_backup(a.path)
    init_data()
    if a.command == "backup":
        dest = Path(a.path) if a.path else BACKUP_DIR / f"hub-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        make_backup_zip(dest)
        return print("Backup written:", dest)

    Handler.lan = a.lan
    host = "0.0.0.0" if a.lan else "127.0.0.1"
    srv = ThreadingHTTPServer((host, a.port), Handler)
    print("Axiom Worker Hub running.")
    print(f"  Computer : http://localhost:{a.port}/")
    if a.lan:
        print(f"  Phone    : http://{lan_ip()}:{a.port}/?token={lan_token()}")
        print("  (LAN mode: same Wi-Fi only; traffic is plain HTTP; do not port-forward this.)")
    print(f"  Data dir : {DATA_DIR}")
    print("  Stop     : press Ctrl+C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
