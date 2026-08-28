"""Agent adapter interface for live evals.

Fixture-mode cases never import this module's concrete adapters. Live-mode
cases use them to invoke a real coding agent against a prompt and a
fixture, then hand whatever project it produced to the same assertion
library used for fixture cases — the eval format does not change per agent.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AgentRunResult:
    agent: str
    model: str | None
    workspace: Path
    duration_s: float
    success: bool
    returncode: int | None = None
    command: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    version: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int | float | None] = field(default_factory=dict)
    cost_usd: float | None = None
    final_message: str | None = None
    permissions: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def normalized(self, *, include_streams: bool = True) -> dict[str, Any]:
        """Return the vendor-neutral record persisted by the eval runner."""
        payload: dict[str, Any] = {
            "schema": "openmapstack-agent-run/v1",
            "agent": self.agent,
            "model": self.model,
            "version": self.version,
            "success": self.success,
            "returncode": self.returncode,
            "command": self.command,
            "duration_s": self.duration_s,
            "event_count": len(self.events),
            "usage": self.usage,
            "cost_usd": self.cost_usd,
            "final_message": self.final_message,
            "permissions": self.permissions,
            "metadata": self.metadata,
        }
        if include_streams:
            payload.update(
                {
                    "events": self.events,
                    "stdout": self.stdout,
                    "stderr": self.stderr,
                }
            )
        return payload


class AgentAdapter:
    """Base class for pluggable agent adapters (Claude Code, Codex, ...).

    Concrete adapters implement `run`. The eval runner never assumes a
    specific vendor: it only calls this interface and then reuses
    `evals/assertions/*` against `AgentRunResult.workspace`.
    """

    name = "base"
    executable = ""

    def is_available(self) -> bool:
        return bool(self.executable and shutil.which(self.executable))

    def run(
        self,
        prompt: str,
        workspace: Path,
        fixture: Path | None = None,
        timeout_s: int = 900,
        model: str | None = None,
        seed: int | None = None,
    ) -> AgentRunResult:
        raise NotImplementedError

    def _timed(self, fn, *args, **kwargs) -> tuple[Any, float]:
        start = time.monotonic()
        result = fn(*args, **kwargs)
        return result, time.monotonic() - start

    def cli_version(self, executable_path: str) -> str | None:
        """Read the exact installed CLI version without turning lookup into a run failure."""
        try:
            proc = subprocess.run(
                [executable_path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        output = (proc.stdout or proc.stderr).strip()
        return output or None


def parse_json_lines(stdout: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse a CLI JSONL stream while retaining malformed/non-JSON lines as evidence."""
    import json

    events: list[dict[str, Any]] = []
    unparsed: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            unparsed.append(line)
            continue
        if isinstance(event, dict):
            events.append(event)
        else:
            unparsed.append(line)
    return events, unparsed
