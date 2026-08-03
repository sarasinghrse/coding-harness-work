"""Turns a GitHub URL into a local, up-to-date clone the harness can point
`tools.configure_workspace()` at. Cloned repos live under repo_cache/,
keyed by owner/repo, so re-running against the same URL just pulls instead
of re-cloning. Uses GITHUB_TOKEN transiently if set (works for private
repos too) but never persists it into the clone's stored git config.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

CACHE_ROOT = Path(__file__).resolve().parent / "repo_cache"

_URL_PATTERN = re.compile(
    r"^(?:https?://)?github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$"
)


class RepoSyncError(Exception):
    pass


def parse_repo_slug(url: str) -> str:
    """Extract 'owner/repo' from a GitHub URL. Raises RepoSyncError if the
    URL doesn't look like a GitHub repo URL."""
    match = _URL_PATTERN.match(url.strip())
    if not match:
        raise RepoSyncError(
            f"{url!r} doesn't look like a GitHub repo URL "
            "(expected something like https://github.com/owner/repo)"
        )
    return f"{match.group(1)}/{match.group(2)}"


def _run_git(args: list[str], cwd: Path, *, redact: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=120,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if redact:
            stderr = stderr.replace(redact, "***")
        raise RepoSyncError(f"git {' '.join(args)} failed: {stderr}")
    return result.stdout.strip()


def sync_repo(url: str) -> tuple[Path, str]:
    """Clone (or pull, if already cached) the given GitHub repo. Returns
    (local_path, repo_slug)."""
    repo_slug = parse_repo_slug(url)
    local_path = CACHE_ROOT / repo_slug.replace("/", "__")
    token = os.environ.get("GITHUB_TOKEN")
    clean_url = f"https://github.com/{repo_slug}.git"
    auth_url = f"https://x-access-token:{token}@github.com/{repo_slug}.git" if token else clean_url

    if not local_path.exists():
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        _run_git(["clone", auth_url, str(local_path)], cwd=CACHE_ROOT, redact=token)
        if token:
            # Don't leave the token sitting in .git/config on disk — reuse it
            # transiently for pulls instead, same pattern as git_publish.py.
            _run_git(["remote", "set-url", "origin", clean_url], cwd=local_path)
    else:
        default_branch = _run_git(
            ["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=local_path
        ).rsplit("/", 1)[-1]
        _run_git(["checkout", default_branch], cwd=local_path)
        _run_git(["pull", auth_url, default_branch], cwd=local_path, redact=token)

    return local_path, repo_slug
