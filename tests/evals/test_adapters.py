from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "evals"))

from adapters.claude_code import ClaudeCodeAdapter  # noqa: E402
from adapters.codex import CodexAdapter  # noqa: E402


class AdapterContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="open-gis-adapter-test-")
        self.workspace = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_codex_normalizes_jsonl_usage_and_final_message(self) -> None:
        stream = "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "Project created."},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 120,
                            "cached_input_tokens": 20,
                            "output_tokens": 30,
                        },
                    }
                ),
            ]
        )

        def run(command, **kwargs):
            if command[-1] == "--version":
                return subprocess.CompletedProcess(command, 0, "codex-cli 0.150.1\n", "")
            self.assertIn("--json", command)
            self.assertIn("--ephemeral", command)
            self.assertEqual(command[-1], "build it")
            return subprocess.CompletedProcess(command, 0, stream, "")

        with (
            patch("adapters.codex.shutil.which", return_value="/bin/codex"),
            patch("adapters.codex.subprocess.run", side_effect=run),
            patch("adapters.base.subprocess.run", side_effect=run),
        ):
            result = CodexAdapter().run("build it", self.workspace, model="gpt-5.6")

        self.assertTrue(result.success)
        self.assertEqual(result.version, "codex-cli 0.150.1")
        self.assertEqual(result.model, "gpt-5.6")
        self.assertEqual(result.final_message, "Project created.")
        self.assertEqual(result.usage["total_tokens"], 150)
        self.assertEqual(result.command[-1], "<PROMPT:prompt.md>")
        self.assertNotIn("build it", result.command)
        self.assertEqual(result.permissions["sandbox"], "workspace-write")

    def test_codex_requires_a_structured_completion_event(self) -> None:
        def run(command, **kwargs):
            if command[-1] == "--version":
                return subprocess.CompletedProcess(command, 0, "codex-cli test\n", "")
            return subprocess.CompletedProcess(command, 0, json.dumps({"type": "turn.started"}) + "\n", "")

        with (
            patch("adapters.codex.shutil.which", return_value="/bin/codex"),
            patch("adapters.codex.subprocess.run", side_effect=run),
            patch("adapters.base.subprocess.run", side_effect=run),
        ):
            result = CodexAdapter().run("build it", self.workspace, model="gpt-test")

        self.assertFalse(result.success)
        self.assertIn("without a turn.completed event", result.stderr)

    def test_claude_normalizes_stream_result_cost_usage_and_actual_model(self) -> None:
        stream = "\n".join(
            [
                json.dumps(
                    {
                        "type": "system",
                        "subtype": "init",
                        "model": "claude-test-20260801",
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": "Project created.",
                        "total_cost_usd": 0.42,
                        "usage": {
                            "input_tokens": 10,
                            "cache_read_input_tokens": 100,
                            "output_tokens": 40,
                        },
                        "modelUsage": {"claude-test-20260801": {"costUSD": 0.42}},
                    }
                ),
            ]
        )

        def run(command, **kwargs):
            if command[-1] == "--version":
                return subprocess.CompletedProcess(command, 0, "2.1.246 (Claude Code)\n", "")
            self.assertIn("stream-json", command)
            self.assertIn("--safe-mode", command)
            self.assertIn("--no-session-persistence", command)
            return subprocess.CompletedProcess(command, 0, stream, "")

        with (
            patch("adapters.claude_code.shutil.which", return_value="/bin/claude"),
            patch("adapters.claude_code.subprocess.run", side_effect=run),
            patch("adapters.base.subprocess.run", side_effect=run),
        ):
            result = ClaudeCodeAdapter().run("build it", self.workspace, model="claude-alias")

        self.assertTrue(result.success)
        self.assertEqual(result.model, "claude-test-20260801")
        self.assertEqual(result.version, "2.1.246 (Claude Code)")
        self.assertEqual(result.cost_usd, 0.42)
        self.assertEqual(result.usage["total_tokens"], 150)
        self.assertEqual(result.final_message, "Project created.")
        self.assertEqual(result.metadata["models_observed"], ["claude-test-20260801"])
        self.assertEqual(result.permissions["mode"], "acceptEdits")

    def test_timeout_retains_partial_structured_events(self) -> None:
        partial = json.dumps({"type": "turn.started"}) + "\n"

        with (
            patch("adapters.codex.shutil.which", return_value="/bin/codex"),
            patch(
                "adapters.codex.subprocess.run",
                side_effect=subprocess.TimeoutExpired("codex", 1, output=partial),
            ),
        ):
            result = CodexAdapter().run("build it", self.workspace, model="gpt-test", timeout_s=1)

        self.assertFalse(result.success)
        self.assertTrue(result.metadata["timed_out"])
        self.assertEqual(result.events[0]["type"], "turn.started")


if __name__ == "__main__":
    unittest.main()
