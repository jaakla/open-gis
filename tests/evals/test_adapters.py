from __future__ import annotations

import json
import os
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
from adapters.openai_compatible import OpenaiCompatibleAdapter  # noqa: E402


def _chat_response(*, content=None, tool_calls=None, model="vendor/model-x", usage=None, cost=None):
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    response = {"id": "chatcmpl-1", "model": model, "choices": [{"index": 0, "message": message, "finish_reason": "stop"}]}
    if usage is not None:
        response["usage"] = dict(usage, **({"cost": cost} if cost is not None else {}))
    return response


class AdapterContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="openmapstack-adapter-test-")
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

    def test_openai_compatible_tool_loop_writes_files_and_finishes(self) -> None:
        responses = [
            _chat_response(
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"path": "project/project.yaml", "content": "schema: openmapstack-project/v1\n"}),
                        },
                    },
                    {
                        "id": "call-2",
                        "type": "function",
                        "function": {
                            "name": "run_shell",
                            "arguments": json.dumps({"command": "echo hello > note.txt"}),
                        },
                    },
                ],
                usage={"prompt_tokens": 100, "completion_tokens": 20},
                cost=0.0031,
            ),
            _chat_response(content="Project created.", usage={"prompt_tokens": 150, "completion_tokens": 30, "total_tokens": 180}, cost=0.0042),
        ]

        with patch.dict(
            os.environ,
            {
                "OPENAI_COMPATIBLE_BASE_URL": "https://openrouter.example/api/v1",
                "OPENAI_COMPATIBLE_API_KEY": "sk-or-secret-test-key",
                "OPENAI_COMPATIBLE_MODEL": "vendor/model-x",
            },
        ):
            adapter = OpenaiCompatibleAdapter()
            self.assertTrue(adapter.is_available())
            with patch.object(adapter, "_post_chat", side_effect=responses) as post:
                result = adapter.run("build it", self.workspace, model=None)

        self.assertTrue(result.success)
        self.assertEqual(result.model, "vendor/model-x")
        self.assertEqual(result.final_message, "Project created.")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.usage["total_tokens"], 300)
        self.assertEqual(result.usage["prompt_tokens"], 250)
        self.assertEqual(result.usage["completion_tokens"], 50)
        self.assertAlmostEqual(result.cost_usd, 0.0073, places=10)
        self.assertEqual(result.metadata["models_observed"], ["vendor/model-x"])
        self.assertEqual(result.metadata["model_source"], "env:OPENAI_COMPATIBLE_MODEL")
        self.assertEqual(result.metadata["turns"], 2)
        self.assertEqual(result.permissions["sandbox"], "workspace-write")
        self.assertEqual((self.workspace / "project" / "project.yaml").read_text(), "schema: openmapstack-project/v1\n")
        self.assertEqual((self.workspace / "note.txt").read_text().strip(), "hello")
        self.assertEqual(result.command, ["POST", "https://openrouter.example/api/v1/chat/completions", "<PROMPT:prompt.md>"])
        # The API key is a secret: it must never appear in any persisted record.
        serialized = json.dumps(result.normalized(), default=str) + json.dumps(result.command)
        self.assertNotIn("sk-or-secret-test-key", serialized)
        tool_results = [event for event in result.events if event["type"] == "tool.result"]
        self.assertEqual([event["ok"] for event in tool_results], [True, True])
        self.assertEqual(post.call_args_list[0].args[2]["model"], "vendor/model-x")

    def test_openai_compatible_flagged_model_overrides_env_default(self) -> None:
        responses = [_chat_response(content="done", usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})]

        with patch.dict(
            os.environ,
            {
                "OPENAI_COMPATIBLE_BASE_URL": "https://api.example/v1",
                "OPENAI_COMPATIBLE_API_KEY": "k",
                "OPENAI_COMPATIBLE_MODEL": "env/model",
            },
        ):
            adapter = OpenaiCompatibleAdapter()
            with patch.object(adapter, "_post_chat", side_effect=responses) as post:
                result = adapter.run("build it", self.workspace, model="flag/model")

        self.assertTrue(result.success)
        self.assertEqual(result.model, "flag/model")
        self.assertEqual(result.metadata["model_source"], "requested")
        self.assertEqual(post.call_args_list[0].args[2]["model"], "flag/model")

    def test_openai_compatible_requires_environment_configuration(self) -> None:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("OPENAI_COMPATIBLE_")
        }
        with patch.dict(os.environ, env, clear=True):
            adapter = OpenaiCompatibleAdapter()
            self.assertFalse(adapter.is_available())
            result = adapter.run("build it", self.workspace)

        self.assertFalse(result.success)
        self.assertTrue(result.metadata["adapter_not_started"])
        self.assertIn("OPENAI_COMPATIBLE_API_KEY", result.stderr)
        self.assertIn("OPENAI_COMPATIBLE_BASE_URL", result.stderr)

    def test_openai_compatible_rejects_workspace_escape(self) -> None:
        responses = [
            _chat_response(
                tool_calls=[
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"path": "../escaped.txt", "content": "nope"}),
                        },
                    }
                ]
            ),
            _chat_response(content="gave up", usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}),
        ]

        with patch.dict(
            os.environ,
            {
                "OPENAI_COMPATIBLE_BASE_URL": "https://api.example/v1",
                "OPENAI_COMPATIBLE_API_KEY": "k",
                "OPENAI_COMPATIBLE_MODEL": "m",
            },
        ):
            adapter = OpenaiCompatibleAdapter()
            with patch.object(adapter, "_post_chat", side_effect=responses):
                result = adapter.run("build it", self.workspace)

        self.assertFalse((self.workspace.parent / "escaped.txt").exists())
        escape_results = [event for event in result.events if event["type"] == "tool.result"]
        self.assertEqual(len(escape_results), 1)
        self.assertFalse(escape_results[0]["ok"])
        self.assertIn("escapes the workspace", escape_results[0]["output"])

    def test_openai_compatible_reports_http_failure_as_setup_error(self) -> None:
        import urllib.error

        with patch.dict(
            os.environ,
            {
                "OPENAI_COMPATIBLE_BASE_URL": "https://api.example/v1",
                "OPENAI_COMPATIBLE_API_KEY": "k",
                "OPENAI_COMPATIBLE_MODEL": "m",
            },
        ):
            adapter = OpenaiCompatibleAdapter()
            with patch.object(
                adapter,
                "_post_chat",
                side_effect=urllib.error.HTTPError("https://api.example/v1/chat/completions", 401, "Unauthorized", {}, None),
            ):
                result = adapter.run("build it", self.workspace)

        self.assertFalse(result.success)
        self.assertIsNone(result.returncode)
        self.assertIn("HTTP 401", result.stderr)

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
