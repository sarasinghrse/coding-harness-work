"""Git branch + PR publishing for the Coding Agent Harness.

Runs only after tests pass. Never touches the default branch directly —
creates a new branch, commits only the diffs a human already approved,
pushes, and opens a PR. Merging is a separate, deliberate human action;
this module never merges anything.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from github import Github


class GitPublishError(Exception):
    pass


def _run_git(args: list[str], cwd: Path, *, redact: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        shown_args = args if redact is None else [a.replace(redact, "***") for a in args]
        stderr = result.stderr.strip()
        if redact:
            stderr = stderr.replace(redact, "***")
        raise GitPublishError(f"git {' '.join(shown_args)} failed: {stderr}")
    return result.stdout.strip()


def publish_pr(
    repo_root: Path,
    applied_diffs: list[dict],
    objective: str,
    thread_id: str,
) -> str:
    """Commit applied diffs to a new branch, push, open a PR. Returns the PR URL."""
    token = os.environ["GITHUB_TOKEN"]
    repo_slug = os.environ["GITHUB_REPO"]

    gh_repo = Github(token).get_repo(repo_slug)
    base_branch = gh_repo.default_branch
    branch_name = f"harness/{thread_id[:8]}"
    remote_url = f"https://x-access-token:{token}@github.com/{repo_slug}.git"

    _run_git(["fetch", "origin", base_branch], cwd=repo_root)
    _run_git(["checkout", "-B", branch_name, f"origin/{base_branch}"], cwd=repo_root)

    changed_files = [diff["file_path"] for diff in applied_diffs]
    _run_git(["add", *changed_files], cwd=repo_root)

    commit_lines = [f"Fix: {objective}"[:72], ""]
    for diff in applied_diffs:
        commit_lines.append(f"- {diff['file_path']}: {diff['rationale']}")
    _run_git(
        [
            "-c", "user.email=harness@local",
            "-c", "user.name=Coding Agent Harness",
            "commit", "-m", "\n".join(commit_lines),
        ],
        cwd=repo_root,
    )
    _run_git(["push", remote_url, f"HEAD:refs/heads/{branch_name}"], cwd=repo_root, redact=token)

    pr_body_lines = [
        f"**Objective:** {objective}",
        "",
        "Each change below was proposed by an LLM, reviewed and approved by a "
        "human, and verified by a real test run in an isolated sandbox before "
        "this PR was opened.",
        "",
    ]
    for diff in applied_diffs:
        pr_body_lines.append(f"### `{diff['file_path']}`")
        pr_body_lines.append(diff["rationale"])
        pr_body_lines.append("")

    pr = gh_repo.create_pull(
        title=f"[harness] {objective}"[:72],
        body="\n".join(pr_body_lines),
        head=branch_name,
        base=base_branch,
    )
    return pr.html_url


def notify_issue(issue_number: int, message: str) -> None:
    """Post a status comment on a GitHub issue — used to report a resumed
    run's outcome back to whichever issue originally triggered it, even when
    the resume happens in a completely different process (cli.py, Streamlit)
    than the one that paused it (webhook_server.py)."""
    token = os.environ["GITHUB_TOKEN"]
    repo_slug = os.environ["GITHUB_REPO"]
    Github(token).get_repo(repo_slug).get_issue(issue_number).create_comment(message)
