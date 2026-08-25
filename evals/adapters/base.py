"""Agent adapter interface for live evals.

Fixture-mode cases never import this module's concrete adapters. Live-mode
cases use them to invoke a real coding agent against a prompt and a
fixture, then hand whatever project it produced to the same assertion
library used for fixture cases — the eval format does not change per agent.
"""

from __future__ import annotations

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
    stdout: str = ""
    stderr: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class AgentAdapter:
    """Base class for pluggable agent adapters (Claude Code, Codex, ...).

    Concrete adapters implement `run`. The eval runner never assumes a
    specific vendor: it only calls this interface and then reuses
    `evals/assertions/*` against `AgentRunResult.workspace`.
    """

    name = "base"

    def run(self, prompt: str, workspace: Path, fixture: Path | None = None, timeout_s: int = 900) -> AgentRunResult:
        raise NotImplementedError

    def _timed(self, fn, *args, **kwargs) -> tuple[Any, float]:
        start = time.monotonic()
        result = fn(*args, **kwargs)
        return result, time.monotonic() - start
