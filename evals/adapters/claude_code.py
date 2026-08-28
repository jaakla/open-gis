"""Claude Code adapter for live evals.

Invokes the `claude` CLI non-interactively against a case prompt inside the
workspace. Requires `claude` on PATH and an authenticated account/API key —
this module is only imported by `--mode live` runs, never by fixture CI.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from .base import AgentAdapter, AgentRunResult, parse_json_lines


def _claude_observability(events: list[dict], requested_model: str | None) -> tuple[str | None, dict, float | None, str | None, bool]:
    result_event = next((event for event in reversed(events) if event.get("type") == "result"), None)
    resolved_model: str | None = None
    for event in events:
        if event.get("type") == "system" and isinstance(event.get("model"), str):
            resolved_model = event["model"]
        message = event.get("message")
        if isinstance(message, dict) and isinstance(message.get("model"), str):
            resolved_model = message["model"]

    usage: dict[str, int | float | None] = {}
    cost_usd: float | None = None
    final_message: str | None = None
    completed = False
    if isinstance(result_event, dict):
        completed = result_event.get("subtype") == "success" and not result_event.get("is_error", False)
        raw_usage = result_event.get("usage")
        if isinstance(raw_usage, dict):
            usage.update({key: value for key, value in raw_usage.items() if isinstance(value, (int, float)) and not isinstance(value, bool)})
        if isinstance(result_event.get("total_cost_usd"), (int, float)):
            cost_usd = float(result_event["total_cost_usd"])
        if isinstance(result_event.get("result"), str):
            final_message = result_event["result"]
        model_usage = result_event.get("modelUsage") or result_event.get("model_usage")
        if isinstance(model_usage, dict) and len(model_usage) == 1:
            resolved_model = next(iter(model_usage))

    if "total_tokens" not in usage:
        token_fields = (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
        )
        values = [usage.get(field) for field in token_fields]
        if any(isinstance(value, (int, float)) for value in values):
            usage["total_tokens"] = sum(value for value in values if isinstance(value, (int, float)))
    return resolved_model or requested_model, usage, cost_usd, final_message, completed


class ClaudeCodeAdapter(AgentAdapter):
    name = "claude_code"
    executable = "claude"

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
                stderr="`claude` CLI not found on PATH",
                permissions={"mode": "acceptEdits", "session_persistence": False},
                metadata={"executable": self.executable, "requested_seed": seed},
            )

        if fixture is not None and fixture.exists():
            shutil.copytree(fixture, workspace, dirs_exist_ok=True)

        version = self.cli_version(executable_path)
        start = time.monotonic()
        command = [
            self.executable,
            "--safe-mode",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "acceptEdits",
            "--no-session-persistence",
            "--no-chrome",
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
        resolved_model, usage, cost_usd, final_message, completed = _claude_observability(events, model)
        models_observed: set[str] = set()
        for event in events:
            if isinstance(event.get("model"), str):
                models_observed.add(event["model"])
            message = event.get("message")
            if isinstance(message, dict) and isinstance(message.get("model"), str):
                models_observed.add(message["model"])
            model_usage = event.get("modelUsage") or event.get("model_usage")
            if isinstance(model_usage, dict):
                models_observed.update(str(name) for name in model_usage)
        success = success and completed and bool(resolved_model) and bool(version)
        if returncode == 0 and not completed:
            stderr = (stderr + "\n" if stderr else "") + ("Claude Code exited successfully without a successful result event")
        if returncode == 0 and not resolved_model:
            stderr = (stderr + "\n" if stderr else "") + "Claude model identity is unresolved"
        if returncode == 0 and not version:
            stderr = (stderr + "\n" if stderr else "") + "Claude Code version is unresolved"

        return AgentRunResult(
            agent=self.name,
            model=resolved_model,
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
            cost_usd=cost_usd,
            final_message=final_message,
            permissions={
                "mode": "acceptEdits",
                "session_persistence": False,
                "chrome": False,
                "customizations": False,
            },
            metadata={
                "executable": executable_path,
                "requested_seed": seed,
                "timed_out": timed_out,
                "timeout_s": timeout_s,
                "requested_model": model,
                "models_observed": sorted(models_observed),
                "model_source": ("event" if models_observed else "requested" if model else "unresolved_cli_default"),
                "structured_completion": completed,
                "unparsed_stdout_lines": unparsed_lines,
                "cost_available": cost_usd is not None,
                "cost_is_estimate": cost_usd is not None,
            },
        )
