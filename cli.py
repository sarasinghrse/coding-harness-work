"""Terminal demo and interactive runner for the Coding Agent Harness."""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

import tools
from demo_data import DEMO_DIFF, DEMO_OBJECTIVE
from git_publish import notify_issue
from graph import DEFAULT_MAX_ITERATIONS, build_graph

PROJECT_ROOT = Path(__file__).resolve().parent

load_dotenv(PROJECT_ROOT / ".env")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the human-gated coding agent harness in your terminal."
    )
    parser.add_argument(
        "--objective",
        default=DEMO_OBJECTIVE,
        help="Ticket for the coding crew. Defaults to the seeded cart bug.",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=DEFAULT_MAX_ITERATIONS,
        help=f"Maximum propose/review/test loops (default: {DEFAULT_MAX_ITERATIONS}).",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Run an offline walkthrough with no API calls or file writes.",
    )
    parser.add_argument(
        "--preview-decision",
        choices=("approve", "reject"),
        help="Non-interactive decision for --preview, useful when recording a demo.",
    )
    parser.add_argument(
        "--skip-baseline",
        action="store_true",
        help="Do not run the local seeded pytest suite before the agent starts.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Path to a real repo to work on. Defaults to the bundled workspace/ demo.",
    )
    parser.add_argument(
        "--test-path",
        default="tests",
        help="Test directory, relative to --repo (default: tests).",
    )
    parser.add_argument(
        "--publish-pr",
        action="store_true",
        help="On success, open a real PR on GITHUB_REPO with the applied diffs "
        "(requires GITHUB_TOKEN/GITHUB_REPO in .env). Off by default.",
    )
    parser.add_argument(
        "--resume",
        default=None,
        metavar="THREAD_ID",
        help="Resume a run that was paused at review and survived a restart "
        "(checkpoints.db is SQLite-backed). Skips planner/explorer/coder.",
    )
    return parser.parse_args()


def render_header(console: Console, *, preview: bool) -> None:
    mode = "OFFLINE WALKTHROUGH" if preview else "LIVE LANGGRAPH RUN"
    title = Text("CODE HARNESS", style="bold white")
    title.append("  /  ", style="dim")
    title.append(mode, style="bold red")
    console.print()
    console.print(title)
    console.print("Review AI code before it touches disk.", style="bold")
    console.print(
        "Planner → Explorer → Coder → [bold red]YOU[/bold red] → Tester",
        style="dim",
    )
    console.print(Rule(style="grey35"))


def render_ticket(console: Console, objective: str) -> None:
    ticket = Text()
    ticket.append("CART-104  BUG\n", style="bold red")
    ticket.append("Cart totals are wrong in production\n", style="bold")
    ticket.append(objective, style="grey70")
    console.print(Panel(ticket, title="Seeded ticket", border_style="grey35"))


def render_pipeline(console: Console) -> None:
    table = Table(show_header=False, box=None, expand=True, padding=(0, 1))
    for _ in range(5):
        table.add_column(ratio=1)
    table.add_row(
        "[dim]01[/dim]\n[bold]Planner[/bold]\nscope work",
        "[dim]02[/dim]\n[bold]Explorer[/bold]\nread repo",
        "[dim]03[/dim]\n[bold]Coder[/bold]\npropose diff",
        "[red]04[/red]\n[bold red]You[/bold red]\napprove/reject",
        "[dim]05[/dim]\n[bold]Tester[/bold]\nE2B pytest",
    )
    console.print(table)
    console.print()


def _notify_origin(console: Console, issue_number: int, message: str) -> None:
    """Best-effort: report a resumed run's outcome back to the GitHub issue
    that triggered it, if this thread came from webhook_server.py."""
    try:
        notify_issue(issue_number, message)
        console.print(f"[dim]Notified originating issue #{issue_number}[/dim]")
    except Exception as exc:
        console.print(f"[yellow]Could not notify issue #{issue_number}: {exc}[/yellow]")


def run_baseline(console: Console, test_path: str) -> None:
    console.print("[bold]Baseline[/bold]  Running the seeded suite locally…")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", test_path, "-q", "--tb=no"],
            cwd=tools.WORKSPACE_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        console.print(f"[yellow]Could not run the baseline suite: {exc}[/yellow]")
        return

    output = (result.stdout + result.stderr).strip()
    border = "red" if result.returncode else "green"
    title = "Expected starting state" if result.returncode else "Current workspace state"
    console.print(
        Panel(
            Syntax(output, "text", word_wrap=True),
            title=title,
            subtitle="The live tester runs in E2B after approval",
            border_style=border,
        )
    )


def render_plan(console: Console, plan: str) -> None:
    if plan:
        console.print(Panel(plan, title="Planner", border_style="cyan"))


def render_diff(console: Console, diff: dict, *, preview: bool = False) -> None:
    mode = "preview, no file writes" if preview else "execution paused"
    heading = Text()
    heading.append(f"{diff['file_path']}\n", style="bold")
    heading.append(diff.get("rationale", "Proposed implementation patch"), style="grey70")
    console.print(Panel(heading, title=f"Proposed file change · {mode}", border_style="red"))
    console.print(
        Syntax(
            diff["unified_diff"],
            "diff",
            theme="ansi_dark",
            line_numbers=True,
            word_wrap=True,
        )
    )


def collect_decisions(console: Console, payload: dict) -> dict:
    decisions: dict[str, dict[str, str]] = {}
    iteration = payload["iteration"] + 1
    console.print(
        f"\n[bold red]HUMAN GATE[/bold red]  Iteration {iteration}. "
        "Nothing below has been written to disk."
    )
    for diff in payload["diffs"]:
        render_diff(console, diff)
        approved = Confirm.ask("Approve this patch?", default=False)
        if approved:
            decisions[diff["diff_id"]] = {"action": "approve", "reason": ""}
            console.print("[green]Approved.[/green] The graph may now apply this diff.")
        else:
            reason = Prompt.ask(
                "What should the coder change?",
                default="Keep the public API intact and make the smallest safe fix.",
            )
            decisions[diff["diff_id"]] = {"action": "reject", "reason": reason}
            console.print("[yellow]Rejected.[/yellow] Feedback will return to the coder.")
    return decisions


def stream_until_pause(console: Console, graph, invoke_arg, config: dict) -> dict | None:
    """Stream node completions and return the next human-review payload, if any."""
    review_payload = None
    for update in graph.stream(invoke_arg, config, stream_mode="updates"):
        if "__interrupt__" in update:
            interrupts = update["__interrupt__"]
            if interrupts:
                review_payload = interrupts[0].value
            continue

        if "planner" in update:
            console.print("[green]✓[/green] Planner scoped the ticket")
            render_plan(console, update["planner"].get("plan", ""))
        elif "explorer" in update:
            console.print("[green]✓[/green] Explorer mapped the relevant workspace")
        elif "coder" in update:
            count = len(update["coder"].get("pending_diffs", []))
            console.print(f"[green]✓[/green] Coder proposed {count} file change(s)")
        elif "apply_diffs" in update:
            count = len(update["apply_diffs"].get("applied_diffs", []))
            console.print(f"[green]✓[/green] Applied {count} human-approved file change(s)")
        elif "tester" in update:
            result = update["tester"]["test_results"][0]
            border = "green" if result["passed"] else "red"
            console.print(
                Panel(
                    Syntax(result["stdout"], "text", word_wrap=True),
                    title=f"E2B pytest · iteration {result['iteration']} · {result['summary']}",
                    border_style=border,
                )
            )
        elif "git_publish" in update:
            pr_url = update["git_publish"].get("pr_url")
            if pr_url and not pr_url.startswith("ERROR:"):
                console.print(f"[green]✓[/green] Opened pull request: {pr_url}")
            elif pr_url:
                console.print(f"[yellow]![/yellow] Could not open a PR: {pr_url}")
    return review_payload


def run_preview(console: Console, args: argparse.Namespace) -> int:
    render_header(console, preview=True)
    render_ticket(console, args.objective)
    render_pipeline(console)
    if not args.skip_baseline:
        run_baseline(console, args.test_path)

    render_plan(
        console,
        "1. Read cart.py and the tests.\n"
        "2. Fix quantity totals, missing-item removal, and discount replacement.\n"
        "3. Change only cart.py.\n"
        "4. Run all five tests in the E2B sandbox.",
    )
    diff = {
        "diff_id": "preview-001",
        "file_path": "cart.py",
        "unified_diff": DEMO_DIFF,
        "rationale": "Fix all three seeded behaviors without changing the tests.",
    }
    render_diff(console, diff, preview=True)

    decision = args.preview_decision
    if decision is None:
        decision = "approve" if Confirm.ask("Approve this preview patch?", default=False) else "reject"
    if decision == "approve":
        console.print(
            Panel(
                "[bold green]APPROVED[/bold green]\n"
                "Live mode would now apply cart.py, upload the workspace to E2B, "
                "and run all five tests.",
                border_style="green",
            )
        )
    else:
        reason = "Preserve the public API and return with a smaller patch."
        if args.preview_decision is None:
            reason = Prompt.ask("Feedback for the coder", default=reason)
        console.print(
            Panel(
                f"[bold yellow]CHANGES REQUESTED[/bold yellow]\n{reason}\n\n"
                "Live mode would send this feedback to the coder and pause again on its revision.",
                border_style="yellow",
            )
        )
    console.print("\n[dim]Preview complete. No API calls were made and no files changed.[/dim]")
    return 0


def run_live(console: Console, args: argparse.Namespace) -> int:
    render_header(console, preview=False)
    render_ticket(console, args.objective)
    render_pipeline(console)

    if args.repo:
        try:
            tools.configure_workspace(args.repo)
        except ValueError as exc:
            console.print(Panel(str(exc), title="Bad --repo path", border_style="red"))
            return 2
        console.print(f"[cyan]●[/cyan] Working on repo: {tools.WORKSPACE_ROOT}")

    if not args.skip_baseline:
        run_baseline(console, args.test_path)

    missing = [key for key in ("GROQ_API_KEY", "E2B_API_KEY") if not os.getenv(key)]
    if missing:
        console.print(
            Panel(
                "Missing: " + ", ".join(missing) + "\n\n"
                "Copy .env.example to .env, add the keys, or run `python cli.py --preview`.",
                title="Cannot start live run",
                border_style="red",
            )
        )
        return 2

    # SQLite-backed instead of InMemorySaver: a paused run now survives a
    # process restart. Save the printed thread id to resume it with --resume.
    conn = sqlite3.connect(str(PROJECT_ROOT / "checkpoints.db"), check_same_thread=False)
    checkpointer = SqliteSaver(conn)
    checkpointer.setup()
    graph = build_graph(checkpointer=checkpointer)

    if args.resume:
        thread_id = args.resume
        config = {"configurable": {"thread_id": thread_id}}
        snapshot = graph.get_state(config)
        if not snapshot or not snapshot.interrupts:
            console.print(
                Panel(
                    f"No pending review found for thread {thread_id}. "
                    "It may already be finished, or never existed.",
                    title="Nothing to resume",
                    border_style="red",
                )
            )
            return 2
        # Restore the workspace this thread was actually running against,
        # unless --repo explicitly overrides it — WORKSPACE_ROOT isn't part
        # of what LangGraph's checkpointer restores automatically, since it
        # lives outside AgentState, so this has to happen by hand.
        saved_root = snapshot.values.get("workspace_root")
        if not args.repo and saved_root:
            tools.configure_workspace(saved_root)
            console.print(f"[cyan]●[/cyan] Restored workspace: {tools.WORKSPACE_ROOT}")
        console.print(f"[cyan]●[/cyan] Resumed thread {thread_id} from checkpoints.db")
        decisions = collect_decisions(console, snapshot.interrupts[0].value)
        invoke_arg: dict | Command = Command(resume={"decisions": decisions})
    else:
        thread_id = str(uuid.uuid4())
        console.print(f"[dim]Thread id (save this to resume later with --resume): {thread_id}[/dim]")
        config = {"configurable": {"thread_id": thread_id}}
        invoke_arg = {
            "objective": args.objective.strip(),
            "iteration": 0,
            "max_iterations": args.max_iterations,
            "test_path": args.test_path,
            "publish_pr": args.publish_pr,
        }
    try:
        console.print("[cyan]●[/cyan] Harness started. Waiting for agent handoffs…")
        while True:
            payload = stream_until_pause(console, graph, invoke_arg, config)
            if payload is None:
                snapshot = graph.get_state(config)
                final_state = snapshot.values if snapshot else {}
                break
            decisions = collect_decisions(console, payload)
            invoke_arg = Command(resume={"decisions": decisions})
    except KeyboardInterrupt:
        console.print("\n[yellow]Run interrupted. No pending diff was applied.[/yellow]")
        return 130
    except Exception as exc:
        console.print(Panel(str(exc), title="Run failed", border_style="red"))
        return 1

    passed = bool(final_state.get("last_test_passed"))
    iterations = final_state.get("iteration", 0)
    origin_issue = final_state.get("origin_issue_number")

    if passed:
        summary = f"[bold green]ALL TESTS PASS[/bold green]\nCompleted in {iterations} iteration(s)."
        pr_url = final_state.get("pr_url")
        if pr_url and not pr_url.startswith("ERROR:"):
            summary += f"\nReview the PR: {pr_url}"
        elif pr_url:
            summary += f"\n[yellow]PR was not opened: {pr_url}[/yellow]"
        console.print(Panel(summary, title="Done", border_style="green"))
        if origin_issue:
            _notify_origin(console, origin_issue, f"✅ Fixed and verified by tests.{f' PR: {pr_url}' if pr_url and not pr_url.startswith('ERROR:') else ''}")
        return 0

    console.print(
        Panel(
            f"[bold red]TESTS STILL FAIL[/bold red]\nStopped after {iterations} iteration(s).",
            title="Iteration limit reached",
            border_style="red",
        )
    )
    if origin_issue:
        _notify_origin(console, origin_issue, f"❌ Gave up after {iterations} iterations; tests still fail.")
    return 1


def main() -> int:
    args = parse_args()
    console = Console()
    if args.preview:
        return run_preview(console, args)
    return run_live(console, args)


if __name__ == "__main__":
    raise SystemExit(main())