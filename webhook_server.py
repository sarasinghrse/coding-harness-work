"""Webhook receiver: a labeled GitHub issue starts a harness run in the
background. Pauses at the same human review gate as every other entry point
(CLI, Streamlit) — the reviewer resumes via `cli.py --resume THREAD_ID` or
the Streamlit app's "Resume a thread" box, both backed by the same durable
SQLite checkpointer, so it doesn't matter that the run started here.

Run with: python -m uvicorn webhook_server:app --port 8787

Required env vars:
  GITHUB_TOKEN            - used both to read the issue and post comments
  GITHUB_REPO             - "owner/repo" this server watches
  GITHUB_WEBHOOK_SECRET   - shared secret configured on the GitHub webhook
  REPO_LOCAL_PATH         - local clone of GITHUB_REPO this server works in
Optional:
  TRIGGER_LABEL           - issue label that starts a run (default: agent-fix)
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import subprocess
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from github import Github
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

import tools
from graph import DEFAULT_MAX_ITERATIONS, build_graph

load_dotenv()

TRIGGER_LABEL = os.environ.get("TRIGGER_LABEL", "agent-fix")
PROJECT_ROOT = Path(__file__).resolve().parent

app = FastAPI()

_conn = sqlite3.connect(str(PROJECT_ROOT / "checkpoints.db"), check_same_thread=False)
_checkpointer = SqliteSaver(_conn)
_checkpointer.setup()
_graph = build_graph(checkpointer=_checkpointer)


def _verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature_header)


def _run_harness_from_issue(objective: str, issue_number: int, thread_id: str) -> None:
    token = os.environ["GITHUB_TOKEN"]
    repo_slug = os.environ["GITHUB_REPO"]

    try:
        gh_issue = Github(token).get_repo(repo_slug).get_issue(issue_number)
    except Exception as exc:  # noqa: BLE001 — can't comment if we can't even fetch the issue
        print(f"webhook: could not fetch issue #{issue_number}: {exc}")
        return

    repo_local_path = os.environ["REPO_LOCAL_PATH"]
    try:
        remote_url = f"https://x-access-token:{token}@github.com/{repo_slug}.git"
        pull = subprocess.run(
            ["git", "pull", remote_url, "main"],
            cwd=repo_local_path,
            timeout=60,
            check=False,
            capture_output=True,
            text=True,
        )
        if pull.returncode != 0:
            stderr = pull.stderr.replace(token, "***")
            raise RuntimeError(f"git pull failed: {stderr.strip()}")
        tools.configure_workspace(repo_local_path)

        config = {"configurable": {"thread_id": thread_id}}
        result = _graph.invoke(
            {
                "objective": objective,
                "iteration": 0,
                "max_iterations": DEFAULT_MAX_ITERATIONS,
                "test_path": "tests",
                "publish_pr": True,
                "origin_issue_number": issue_number,
            },
            config,
        )

        if "__interrupt__" in result:
            payload = result["__interrupt__"][0].value
            files = ", ".join(d["file_path"] for d in payload["diffs"])
            gh_issue.create_comment(
                f"🤖 Proposed a fix for `{files}`. Nothing has been written to disk yet — "
                f"this is paused waiting for human review.\n\n"
                f"Resume it with:\n```\npython cli.py --resume {thread_id}\n```\n"
                f"or the Streamlit app's \"Resume a thread\" box, using thread id `{thread_id}`."
            )
        elif result.get("last_test_passed"):
            pr_note = f"\n\nPR: {result['pr_url']}" if result.get("pr_url") else ""
            gh_issue.create_comment(f"✅ Fixed and verified by tests.{pr_note}")
        else:
            gh_issue.create_comment(
                f"❌ Gave up after {result.get('iteration', 0)} iterations; tests still fail."
            )
    except Exception as exc:  # noqa: BLE001 — always report back to the issue, never fail silently
        gh_issue.create_comment(f"⚠️ Harness run errored: {exc}")


@app.post("/webhook")
async def webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
):
    body = await request.body()
    secret = os.environ["GITHUB_WEBHOOK_SECRET"]
    if not _verify_signature(secret, body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="bad signature")

    if x_github_event != "issues":
        return {"status": "ignored", "reason": "not an issues event"}

    payload = await request.json()
    if payload.get("action") != "labeled":
        return {"status": "ignored", "reason": "not a labeled action"}
    if payload.get("label", {}).get("name") != TRIGGER_LABEL:
        return {"status": "ignored", "reason": "label doesn't match TRIGGER_LABEL"}

    issue = payload["issue"]
    objective = f"{issue['title']}\n\n{issue.get('body') or ''}".strip()
    thread_id = str(uuid.uuid4())

    import asyncio
    asyncio.get_event_loop().run_in_executor(
        None, _run_harness_from_issue, objective, issue["number"], thread_id
    )
    return {"status": "started", "thread_id": thread_id}
