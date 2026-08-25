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

    def run(self, prompt: str, workspace: Path, fixture: Path | None = None, timeout_s: int = 900) -> AgentRunResult:
        if shutil.which("claude") is None:
            return AgentRunResult(
                agent=self.name, model=None, workspace=workspace, duration_s=0.0,
                success=False, stderr="`claude` CLI not found on PATH",
            )

        if fixture is not None and fixture.exists():
            shutil.copytree(fixture, workspace, dirs_exist_ok=True)

        start = time.monotonic()
        try:
            proc = subprocess.run(
                ["claude", "-p", prompt, "--permission-mode", "acceptEdits"],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            success = proc.returncode == 0
            stdout, stderr = proc.stdout, proc.stderr
        except subprocess.TimeoutExpired as exc:
            success = False
            stdout, stderr = exc.stdout or "", f"timed out after {timeout_s}s"
        duration = time.monotonic() - start

        return AgentRunResult(
            agent=self.name, model="claude-code-cli", workspace=workspace,
            duration_s=duration, success=success, stdout=stdout, stderr=stderr,
        )
