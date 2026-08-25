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

from .base import AgentAdapter, AgentRunResult


class CodexAdapter(AgentAdapter):
    name = "codex"

    def run(self, prompt: str, workspace: Path, fixture: Path | None = None, timeout_s: int = 900) -> AgentRunResult:
        if shutil.which("codex") is None:
            return AgentRunResult(
                agent=self.name, model=None, workspace=workspace, duration_s=0.0,
                success=False, stderr="`codex` CLI not found on PATH",
            )

        if fixture is not None and fixture.exists():
            shutil.copytree(fixture, workspace, dirs_exist_ok=True)

        start = time.monotonic()
        try:
            proc = subprocess.run(
                ["codex", "exec", prompt],
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
            agent=self.name, model="codex-cli", workspace=workspace,
            duration_s=duration, success=success, stdout=stdout, stderr=stderr,
        )
