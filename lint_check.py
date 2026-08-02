"""Local static-analysis gate for proposed Python diffs.

Runs `ruff` against proposed file content in isolation, before a human ever
sees the diff — mechanical issues get caught and auto-retried by the coder
without spending a review cycle on something a linter already knows is wrong.
Never runs untrusted code, only static analysis, so this runs locally rather
than in the E2B sandbox.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


def check_python_content(new_content: str) -> str | None:
    """Run ruff against proposed file content. Returns formatted issues, or
    None if clean (or if ruff isn't available — this gate fails open rather
    than blocking the whole loop on a missing local tool)."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(new_content)
        tmp_path = Path(tmp.name)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--quiet", str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return None
        output = (result.stdout + result.stderr).strip()
        return output.replace(str(tmp_path), "<proposed file>")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    finally:
        tmp_path.unlink(missing_ok=True)
