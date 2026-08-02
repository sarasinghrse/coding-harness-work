"""E2B sandbox wrapper: detect the project's language and run its real test
suite in an isolated VM. Supports Python (pytest), Node (npm test), and Go
(go test) by checking for the language's marker file in the workspace root;
falls back to Python/pytest if nothing else matches."""
from __future__ import annotations

from pathlib import Path

from e2b import Sandbox

try:  # raised by commands.run on a non-zero exit code (e.g. failing tests)
    from e2b import CommandExitException
except ImportError:  # older e2b versions export it from the exceptions module
    from e2b.exceptions import CommandExitException  # type: ignore[no-redef]

SANDBOX_WORKDIR = "/home/user/workspace"
IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".git", "node_modules", ".venv"}


def _detect_commands(workspace_root: Path, test_path: str) -> tuple[str, str, str]:
    """Return (language_label, install_cmd, test_cmd) from marker files."""
    if (workspace_root / "package.json").is_file():
        return "node", "npm install --no-audit --no-fund", "npm test --silent"

    if (workspace_root / "go.mod").is_file():
        return "go", "go mod download", "go test ./..."

    install_parts = ["pip install -q pytest"]
    if (workspace_root / "requirements.txt").is_file():
        install_parts.append("pip install -q -r requirements.txt")
    return "python", " && ".join(install_parts), f"python -m pytest {test_path} -q"


def run_tests_in_sandbox(workspace_root: Path, test_path: str = "tests") -> dict:
    """Upload the workspace to a fresh E2B sandbox, run its test suite, report back.

    Returns: {"passed": bool, "summary": str, "stdout": str, "returncode": int}
    """
    language, install_cmd, test_cmd = _detect_commands(workspace_root, test_path)

    sandbox = Sandbox.create(timeout=300)
    try:
        for file in sorted(workspace_root.rglob("*")):
            if not file.is_file() or any(p in IGNORED_PARTS for p in file.parts):
                continue
            rel = file.relative_to(workspace_root)
            try:
                content = file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # skip binary/unreadable files — nothing to test there anyway
            sandbox.files.write(f"{SANDBOX_WORKDIR}/{rel.as_posix()}", content)

        sandbox.commands.run(install_cmd, cwd=SANDBOX_WORKDIR, timeout=180)
        try:
            proc = sandbox.commands.run(test_cmd, cwd=SANDBOX_WORKDIR, timeout=180)
            returncode, stdout, stderr = proc.exit_code, proc.stdout, proc.stderr
        except CommandExitException as exc:
            returncode = exc.exit_code
            stdout, stderr = exc.stdout, exc.stderr
    finally:
        try:
            sandbox.kill()
        except Exception:
            pass

    output = (stdout + ("\n" + stderr if stderr else "")).strip()
    summary_lines = [line for line in stdout.strip().splitlines() if line.strip()]
    return {
        "passed": returncode == 0,
        "summary": summary_lines[-1] if summary_lines else f"exit code {returncode}",
        "stdout": output[-4000:],
        "returncode": returncode,
        "language": language,
    }


# Backward-compatible alias — earlier phases imported this name directly.
run_pytest_in_sandbox = run_tests_in_sandbox
