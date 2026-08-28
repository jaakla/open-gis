"""Codex CLI adapter for live evals.

Invokes the `codex` CLI non-interactively against a case prompt inside the
workspace. Requires `codex` on PATH and configured credentials — only
imported by `--mode live` runs, never by fixture CI.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from .base import AgentAdapter, AgentRunResult, parse_json_lines


def _codex_observability(events: list[dict]) -> tuple[dict, str | None, bool]:
    usage: dict[str, int | float | None] = {}
    final_message: str | None = None
    completed = False
    for event in events:
        if event.get("type") == "turn.failed":
            completed = False
        if event.get("type") == "turn.completed":
            completed = True
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict):
                for key, value in raw_usage.items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        usage[key] = value
        item = event.get("item")
        if event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            final_message = item["text"]
    if "total_tokens" not in usage:
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if isinstance(input_tokens, (int, float)) and isinstance(output_tokens, (int, float)):
            usage["total_tokens"] = input_tokens + output_tokens
    return usage, final_message, completed


class CodexAdapter(AgentAdapter):
    name = "codex"
    executable = "codex"

    def run(
        self,
        prompt: str,
        workspace: Path,
        fixture: Path | None = None,
        timeout_s: int = 900,
        model: str | None = None,
        seed: int | None = None,
    ) -> AgentRunResult:
        executable_path = shutil.which(self.executable)
        if executable_path is None:
            return AgentRunResult(
                agent=self.name,
                model=None,
                workspace=workspace,
                duration_s=0.0,
                success=False,
                returncode=None,
                command=[self.executable],
                stderr="`codex` CLI not found on PATH",
                permissions={
                    "sandbox": "workspace-write",
                    "session_persistence": False,
                },
                metadata={"executable": self.executable, "requested_seed": seed},
            )

        if fixture is not None and fixture.exists():
            shutil.copytree(fixture, workspace, dirs_exist_ok=True)

        version = self.cli_version(executable_path)
        start = time.monotonic()
        command = [
            self.executable,
            "exec",
            "--json",
            "--ephemeral",
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
        ]
        if model:
            command.extend(["--model", model])
        invocation = [*command, prompt]
        recorded_command = [*command, "<PROMPT:prompt.md>"]
        timed_out = False
        try:
            proc = subprocess.run(
                invocation,
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
            success = proc.returncode == 0
            returncode = proc.returncode
            stdout, stderr = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            success = False
            returncode = None
            timed_out = True
            stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
            stderr = f"timed out after {timeout_s}s"
        except OSError as exc:
            success = False
            returncode = None
            stdout = ""
            stderr = f"{type(exc).__name__}: {exc}"
        duration = time.monotonic() - start
        events, unparsed_lines = parse_json_lines(stdout)
        usage, final_message, completed = _codex_observability(events)
        success = success and completed and bool(model) and bool(version)
        if returncode == 0 and not completed:
            stderr = (stderr + "\n" if stderr else "") + ("Codex exited successfully without a turn.completed event")
        if returncode == 0 and not model:
            stderr = (stderr + "\n" if stderr else "") + "Codex model identity is unresolved"
        if returncode == 0 and not version:
            stderr = (stderr + "\n" if stderr else "") + "Codex CLI version is unresolved"

        return AgentRunResult(
            agent=self.name,
            model=model,
            workspace=workspace,
            duration_s=duration,
            success=success,
            returncode=returncode,
            command=recorded_command,
            stdout=stdout,
            stderr=stderr,
            version=version,
            events=events,
            usage=usage,
            cost_usd=None,
            final_message=final_message,
            permissions={
                "sandbox": "workspace-write",
                "session_persistence": False,
                "user_config": False,
                "exec_rules": False,
            },
            metadata={
                "executable": executable_path,
                "requested_seed": seed,
                "timed_out": timed_out,
                "timeout_s": timeout_s,
                "model_source": "requested" if model else "unresolved_cli_default",
                "structured_completion": completed,
                "unparsed_stdout_lines": unparsed_lines,
                "cost_available": False,
            },
        )
