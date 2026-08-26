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

from .base import AgentAdapter, AgentRunResult


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
                agent=self.name, model=None, workspace=workspace, duration_s=0.0,
                success=False, returncode=None, command=[self.executable],
                stderr="`claude` CLI not found on PATH",
                metadata={"executable": self.executable, "requested_seed": seed},
            )

        if fixture is not None and fixture.exists():
            shutil.copytree(fixture, workspace, dirs_exist_ok=True)

        start = time.monotonic()
        command = [self.executable, "-p", "--permission-mode", "acceptEdits"]
        if model:
            command.extend(["--model", model])
        command.append(prompt)
        timed_out = False
        try:
            proc = subprocess.run(
                command,
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=timeout_s,
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
        duration = time.monotonic() - start

        return AgentRunResult(
            agent=self.name, model=model or "claude-code-cli-default", workspace=workspace,
            duration_s=duration, success=success, returncode=returncode,
            command=command, stdout=stdout, stderr=stderr,
            metadata={
                "executable": executable_path,
                "requested_seed": seed,
                "timed_out": timed_out,
                "timeout_s": timeout_s,
            },
        )
