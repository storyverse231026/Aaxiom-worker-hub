# Axiom Worker Hub

A personal, local-first command center for coordinating several AI chat services by manual copy/paste handoff.
No API keys, no passwords, no cookies, no browser automation. Python standard library only.

## Install
1. Install Python 3.9+ (`python3 --version`). macOS: https://www.python.org/downloads/ if missing.
2. Download/clone this repo. That's it, there is nothing else to install.

## Start / stop
```
cd axiom-worker-hub
./start.sh            # this computer only  ->  http://localhost:8787
./start.sh --lan      # also your phone on the same Wi-Fi (prints a URL with a private token)
```
Stop with `Ctrl+C` in the terminal. (If `./start.sh` is not executable: `python3 server.py`.)

## Where your data lives
`~/.axiom-worker-hub/` (outside the repo): database, attachments, backups, LAN token.
Change with the `AXIOM_HUB_DATA` environment variable. `.gitignore` also blocks databases, backups, zips, tokens and `.env` files as a safety net.

## Using it
1. **Workers**: seven providers are pre-registered (Claude, ChatGPT, DeepSeek, Kimi, Perplexity, Grok, Meta AI). Use *Add worker* for more accounts of the same provider. Roles, capabilities and instructions are editable. "Availability" is a manual note; the hub cannot see inside the external apps.
2. **Projects & Memory**: create a project (e.g. Axiom), paste its private rules into *Project instructions*, add memory items (decisions, research, architecture...).
3. **Tasks**: create a task, open it, tick workers, choose context, **Generate prompts**. Only the project instructions plus the memory items you tick (relevant ones are suggested by keyword) go into each prompt.
4. **Handoff**: **Copy prompt**, **Open <provider>**, paste, run it there, paste the answer back, **Save response**. Headings like `## Advantages` / `## Sources` in the answer are auto-filed.
5. **Compare & review**: submissions appear side by side. Annotate conflicts and missing evidence, press **Request peer review** to hand a submission to another worker, or choose *Consolidate all submissions* to get a merged solution.
6. **Decide**: *Approve / Request revisions / Reject*. A task can only become Completed through your approval. Deletions ask for confirmation.

Provider links are well-known homepages that this app has **not** verified; override per worker in *Edit*.

## Phone (Android)
Run `./start.sh --lan`, open the printed URL on your phone (same Wi-Fi). Traffic is plain HTTP on your LAN, protected by the token; never port-forward it. The Copy button falls back to manual select-and-copy on plain-HTTP pages if the browser blocks clipboard access.

## Backup / restore
- UI: **Backup** page, or `python3 server.py backup [file.zip]`
- Restore (stop the hub first): `python3 server.py restore backup.zip` (a safety copy of current data is made first).

## Pre-loading a project privately
Copy `seed.example.json` to `~/.axiom-worker-hub/seed.json` and edit it before the first run. Never put real project rules in the repo.

## Tests
`python3 test_smoke.py` (uses a temporary data folder).

## Status
Tested: workers, projects, memory, tasks/subtasks, prompt generation, submissions, peer review, consolidation, owner approval gate, persistence across restart, backup and restore, LAN token check, `.gitignore`, UI flow in headless Chrome.
Not built: provider API integrations (manual handoff only), in-UI restore, Android-device testing.
