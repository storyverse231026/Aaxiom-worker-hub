#!/usr/bin/env python3
"""End-to-end smoke test. Uses a temporary data folder, never your real data.
Run:  python3 test_smoke.py
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = 18787
BASE = f"http://127.0.0.1:{PORT}"
DATA = tempfile.mkdtemp(prefix="hubtest-")
ENV = {**os.environ, "AXIOM_HUB_DATA": DATA}


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw[:1] in b"{[" else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def start(*extra):
    p = subprocess.Popen([sys.executable, str(ROOT / "server.py"), "--port", str(PORT), *extra],
                         env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(50):
        try:
            urllib.request.urlopen(BASE + "/api/meta").read()
            return p
        except Exception:
            time.sleep(0.1)
    p.kill()
    raise SystemExit("server did not start")


passed = 0


def check(name, cond):
    global passed
    if not cond:
        print("FAIL:", name)
        sys.exit(1)
    passed += 1
    print("ok  ", name)


p = start()
try:
    s, ws = call("GET", "/api/workers")
    check("seven providers registered by default", len({w["provider"] for w in ws}) == 7)
    s, r = call("POST", "/api/workers", {"provider": "claude", "name": "Claude #2"})
    check("multiple accounts per provider", s == 200)
    s, ws = call("GET", "/api/workers")
    check("two claude workers", sum(w["provider"] == "claude" for w in ws) == 2)

    s, r = call("POST", "/api/projects", {"name": "Axiom", "instructions": "RULE-ONE: be careful."})
    pid = r["id"]
    call("POST", f"/api/projects/{pid}/memory", {"kind": "decision", "title": "Orchestration engine notes", "body": "queue based design"})
    call("POST", f"/api/projects/{pid}/memory", {"kind": "note", "title": "Unrelated banana recipe", "body": "yellow fruit"})
    s, r = call("POST", "/api/tasks", {"project_id": pid, "title": "Improve orchestration engine", "description": "Propose improvements"})
    tid = r["id"]
    s, t = call("GET", f"/api/tasks/{tid}")
    check("task starts in Backlog", t["status"] == "Backlog")
    check("memory suggestion is relevant only", len(t["suggested_memory"]) == 1)

    by = {}
    for w in ws:
        by.setdefault(w["provider"], w["id"])
    s, r = call("POST", f"/api/tasks/{tid}/assign", {"kind": "work", "worker_ids": [by["claude"], by["deepseek"], by["kimi"]]})
    check("3 prompts generated", s == 200 and len(r["assignment_ids"]) == 3)
    s, t = call("GET", f"/api/tasks/{tid}")
    check("status Assigned", t["status"] == "Assigned")
    a0 = t["assignments"][0]
    check("prompt has project instructions", "RULE-ONE" in a0["prompt"])
    check("prompt has relevant memory", "queue based design" in a0["prompt"])
    check("prompt omits irrelevant memory", "banana" not in a0["prompt"])

    resp = "## Solution\nUse a queue.\n## Advantages\nSimple\n## Disadvantages\nLatency\n## Sources\nhttps://example.com"
    ids = [a["id"] for a in t["assignments"]]
    call("PUT", f"/api/assignments/{ids[0]}", {"status": "sent"})
    s, t = call("GET", f"/api/tasks/{tid}")
    check("status Working after mark sent", t["status"] == "Working")
    for i in ids:
        s, r = call("POST", f"/api/assignments/{i}/submit", {"body": resp})
        check(f"submission {i} saved", s == 200)
    s, r = call("POST", f"/api/assignments/{ids[0]}/submit", {"body": resp})
    check("duplicate submit rejected", s == 409)
    s, t = call("GET", f"/api/tasks/{tid}")
    check("status Submitted", t["status"] == "Submitted")
    sub = t["submissions"][0]
    check("sections auto-parsed", sub["pros"] == "Simple" and sub["cons"] == "Latency" and "example.com" in sub["sources"])

    s, r = call("POST", f"/api/tasks/{tid}/assign", {"kind": "review", "worker_ids": [by["chatgpt"]], "review_of": sub["id"]})
    check("review assignment", s == 200)
    s, t = call("GET", f"/api/tasks/{tid}")
    rv = [a for a in t["assignments"] if a["kind"] == "review"][0]
    check("review prompt contains submission", "Use a queue." in rv["prompt"])
    call("POST", f"/api/assignments/{rv['id']}/submit", {"body": "## Verdict\nRevise"})
    s, r = call("POST", f"/api/tasks/{tid}/assign", {"kind": "consolidate", "worker_ids": [by["claude"]]})
    check("consolidate assignment", s == 200)
    s, t = call("GET", f"/api/tasks/{tid}")
    cp = [a for a in t["assignments"] if a["kind"] == "consolidate"][0]
    check("consolidate prompt includes review", "Revise" in cp["prompt"])
    call("POST", f"/api/assignments/{cp['id']}/submit", {"body": "Consolidated answer"})
    s, t = call("GET", f"/api/tasks/{tid}")
    check("status Owner Approval", t["status"] == "Owner Approval")

    s, r = call("PUT", f"/api/tasks/{tid}", {"status": "Completed"})
    check("cannot bypass owner approval", s == 403)
    s, r = call("POST", "/api/tasks", {"project_id": pid, "title": "x"})
    s, r = call("DELETE", f"/api/projects/{pid}")
    check("deletion needs owner confirmation", s == 403)

    call("POST", f"/api/tasks/{tid}/attachments", {"filename": "notes.txt", "data": base64.b64encode(b"ATTACHED-TEXT").decode()})
    call("POST", f"/api/tasks/{tid}/comments", {"body": "discussion note"})
    s, r = call("POST", f"/api/tasks/{tid}/decision", {"decision": "approve", "notes": "ok"})
    check("approve -> Completed", r["status"] == "Completed")

    s, d = call("GET", "/api/dashboard")
    check("dashboard counts completed", d["by_status"].get("Completed") == 1)
    s, r = call("POST", "/api/backup")
    check("backup written", Path(r["path"]).exists())
    backup = r["path"]
    s, r = call("POST", "/api/tasks", {"project_id": pid, "title": "Cross-origin test"})
    req = urllib.request.Request(BASE + "/api/tasks", method="POST", data=b"{}",
                                 headers={"Content-Type": "application/json", "Origin": "http://evil.example"})
    try:
        urllib.request.urlopen(req)
        check("cross-origin blocked", False)
    except urllib.error.HTTPError as e:
        check("cross-origin blocked", e.code == 403)
finally:
    p.terminate()
    p.wait()

# persistence across restart
p = start()
try:
    s, ts = call("GET", "/api/tasks")
    check("data survives restart", any(t["title"] == "Improve orchestration engine" and t["status"] == "Completed" for t in ts))
    s, t = call("GET", f"/api/tasks/{ts[-1]['id']}")
finally:
    p.terminate()
    p.wait()

# restore
r = subprocess.run([sys.executable, str(ROOT / "server.py"), "restore", backup], env=ENV, capture_output=True, text=True)
check("restore succeeds", r.returncode == 0)
p = start()
try:
    s, ts = call("GET", "/api/tasks")
    check("restored data present", any(t["title"] == "Improve orchestration engine" for t in ts))
finally:
    p.terminate()
    p.wait()

# LAN token protection (simulated non-local check is covered by code path; verify token file exists with 0600)
cfg = Path(DATA) / "config.json"
check("lan token file created", cfg.exists() and (cfg.stat().st_mode & 0o077) == 0)

# .gitignore check
if (ROOT / ".git").exists():
    for f in ("hub.db", "hub.db-wal", "attachments/x.txt", "backups/b.zip", "config.json", "seed.json", ".env", "data/anything"):
        r = subprocess.run(["git", "check-ignore", "-q", f], cwd=ROOT)
        check(f".gitignore covers {f}", r.returncode == 0)
    r = subprocess.run(["git", "check-ignore", "-q", "seed.example.json"], cwd=ROOT)
    check("seed.example.json is NOT ignored", r.returncode != 0)

print(f"\nALL {passed} CHECKS PASSED")
