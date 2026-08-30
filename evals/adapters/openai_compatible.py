"""OpenAI-compatible chat-completions adapter for live evals.

Drives any OpenAI-compatible `/chat/completions` endpoint (OpenRouter,
vLLM, llama.cpp server, ...) as an autonomous coding agent through a
tool-execution loop: the model writes files and runs shell commands inside
the trial workspace until it produces a final message with no tool calls.

All configuration comes from the environment — no CLI install:

    OPENAI_COMPATIBLE_BASE_URL   API root, e.g. https://openrouter.ai/api/v1
    OPENAI_COMPATIBLE_API_KEY    bearer token (secret; never recorded)
    OPENAI_COMPATIBLE_MODEL      default model id (--model overrides it)

This module is only imported by `--mode live` runs, never by fixture CI.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .base import AgentAdapter, AgentRunResult

MAX_TURNS = 64
MAX_TOOL_OUTPUT_CHARS = 20_000
SHELL_TIMEOUT_S = 120
HTTP_TIMEOUT_S = 300

SYSTEM_PROMPT = """You are an autonomous coding agent working inside a plain \
directory (your workspace). Complete the user's task there. You can run \
shell commands and create, read, and list files with the provided tools. \
Work incrementally: inspect the workspace, write the needed files, run and \
verify your work, and iterate until the task is done. When you are finished, \
reply with a short summary and no tool calls."""


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a shell command inside the workspace and return its combined output and exit status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a workspace-relative text file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative destination path."},
                    "content": {"type": "string", "description": "Full file content."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a workspace-relative file's content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative path to read."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List a workspace-relative directory's entries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative directory (default '.')."},
                },
            },
        },
    },
]


def _truncate(value: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n...[truncated {len(value) - limit} characters]"


def _resolve_in_workspace(workspace: Path, raw: Any) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("path must be a non-empty workspace-relative string")
    resolved_workspace = workspace.resolve()
    candidate = (workspace / raw).resolve()
    if candidate != resolved_workspace and resolved_workspace not in candidate.parents:
        raise ValueError(f"path escapes the workspace: {raw!r}")
    return candidate


class OpenaiCompatibleAdapter(AgentAdapter):
    """Agent adapter for any OpenAI-compatible chat-completions endpoint.

    The class name follows the eval runner's adapter-loading convention
    (`openai_compatible` -> `OpenaiCompatibleAdapter`).
    """

    name = "openai_compatible"
    executable = ""  # no CLI; availability is environment-based

    @staticmethod
    def _env() -> dict[str, str | None]:
        return {
            "base_url": (os.environ.get("OPENAI_COMPATIBLE_BASE_URL") or "").strip().rstrip("/") or None,
            "api_key": (os.environ.get("OPENAI_COMPATIBLE_API_KEY") or "").strip() or None,
            "model": (os.environ.get("OPENAI_COMPATIBLE_MODEL") or "").strip() or None,
        }

    def is_available(self) -> bool:
        env = self._env()
        return bool(env["base_url"] and env["api_key"] and env["model"])

    def _post_chat(self, base_url: str, api_key: str, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))

    def _execute_tool(self, name: str, args: Any, workspace: Path, remaining_s: float) -> tuple[bool, str]:
        if not isinstance(args, dict):
            return False, f"tool {name!r} arguments must be an object, got {type(args).__name__}"
        try:
            if name == "run_shell":
                command = args.get("command")
                if not isinstance(command, str) or not command.strip():
                    return False, "run_shell requires a non-empty 'command' string"
                proc = subprocess.run(  # noqa: S602 - the model authors the command by design
                    command,
                    shell=True,
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    timeout=max(1, min(SHELL_TIMEOUT_S, remaining_s)),
                    check=False,
                )
                output = f"exit={proc.returncode}\n{proc.stdout}{proc.stderr}".strip()
                return proc.returncode == 0, _truncate(output)
            if name == "write_file":
                target = _resolve_in_workspace(workspace, args.get("path"))
                content = args.get("content")
                if not isinstance(content, str):
                    return False, "write_file requires 'content' to be a string"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                return True, f"wrote {len(content)} chars to {target.relative_to(workspace.resolve())}"
            if name == "read_file":
                target = _resolve_in_workspace(workspace, args.get("path"))
                if not target.is_file():
                    return False, f"not a file: {args.get('path')!r}"
                return True, _truncate(target.read_text(encoding="utf-8", errors="replace"))
            if name == "list_dir":
                target = _resolve_in_workspace(workspace, args.get("path") or ".")
                if not target.is_dir():
                    return False, f"not a directory: {args.get('path')!r}"
                entries = []
                for entry in sorted(target.iterdir()):
                    entries.append(entry.name + ("/" if entry.is_dir() else ""))
                return True, _truncate("\n".join(entries))
            return False, f"unknown tool: {name!r}"
        except subprocess.TimeoutExpired:
            return False, f"command timed out after {min(SHELL_TIMEOUT_S, remaining_s):.0f}s"
        except Exception as exc:  # noqa: BLE001 - tool failures are reported to the model
            return False, f"{type(exc).__name__}: {exc}"

    def run(
        self,
        prompt: str,
        workspace: Path,
        fixture: Path | None = None,
        timeout_s: int = 900,
        model: str | None = None,
        seed: int | None = None,
    ) -> AgentRunResult:
        env = self._env()
        base_url, api_key, env_model = env["base_url"], env["api_key"], env["model"]
        requested_model = model or env_model
        missing = [
            name
            for name, value in (
                ("OPENAI_COMPATIBLE_BASE_URL", base_url),
                ("OPENAI_COMPATIBLE_API_KEY", api_key),
                ("OPENAI_COMPATIBLE_MODEL (or --model)", requested_model),
            )
            if not value
        ]

        if missing:
            return AgentRunResult(
                agent=self.name,
                model=requested_model,
                workspace=workspace,
                duration_s=0.0,
                success=False,
                returncode=None,
                command=["POST", "<OPENAI_COMPATIBLE_BASE_URL>/chat/completions", "<PROMPT:prompt.md>"],
                stderr=f"OpenAI-compatible adapter is missing required configuration: {', '.join(missing)}",
                permissions={"sandbox": "workspace-write", "network": True, "session_persistence": False},
                metadata={
                    "requested_seed": seed,
                    "adapter_not_started": True,
                    "missing_environment": missing,
                },
            )

        assert base_url is not None and api_key is not None and requested_model is not None
        if fixture is not None and fixture.exists():
            shutil.copytree(fixture, workspace, dirs_exist_ok=True)

        start = time.monotonic()
        deadline = start + timeout_s
        recorded_command = ["POST", f"{base_url}/chat/completions", "<PROMPT:prompt.md>"]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        events: list[dict[str, Any]] = []
        usage: dict[str, float | int] = {}
        cost_usd: float | None = None
        models_observed: set[str] = set()
        completed = False
        timed_out = False
        final_message: str | None = None
        error_text: str | None = None
        turns = 0

        while turns < MAX_TURNS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                error_text = f"timed out after {timeout_s}s"
                break
            payload = {
                "model": requested_model,
                "messages": messages,
                "tools": TOOLS,
                "tool_choice": "auto",
            }
            try:
                response = self._post_chat(base_url, api_key, payload, timeout_s=min(remaining, HTTP_TIMEOUT_S))
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001 - diagnostic best effort
                    pass
                error_text = f"HTTP {exc.code} from {base_url}/chat/completions: {_truncate(body, 2000) or exc.reason}"
                break
            except Exception as exc:  # noqa: BLE001 - transport errors end the run
                error_text = f"{type(exc).__name__}: {exc}"
                break

            turns += 1
            raw_usage = response.get("usage")
            if isinstance(raw_usage, dict):
                has_split = all(isinstance(raw_usage.get(key), (int, float)) and not isinstance(raw_usage.get(key), bool) for key in ("prompt_tokens", "completion_tokens"))
                for key in ("prompt_tokens", "completion_tokens"):
                    value = raw_usage.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        usage[key] = usage.get(key, 0) + value
                total = raw_usage.get("total_tokens")
                if not has_split and isinstance(total, (int, float)) and not isinstance(total, bool):
                    usage["total_tokens"] = usage.get("total_tokens", 0) + total
                provider_cost = raw_usage.get("cost")
                if isinstance(provider_cost, (int, float)) and not isinstance(provider_cost, bool):
                    cost_usd = (cost_usd or 0.0) + float(provider_cost)
            if isinstance(response.get("model"), str):
                models_observed.add(response["model"])

            choices = response.get("choices")
            message = choices[0].get("message") if isinstance(choices, list) and choices else None
            if not isinstance(message, dict):
                error_text = "response contained no assistant message"
                break

            content = message.get("content")
            tool_calls = message.get("tool_calls") or []
            assistant_entry: dict[str, Any] = {"role": "assistant", "content": content if isinstance(content, str) else ""}
            if tool_calls:
                assistant_entry["tool_calls"] = tool_calls
            messages.append(assistant_entry)
            events.append(
                {
                    "type": "assistant.message",
                    "turn": turns,
                    "content": _truncate(assistant_entry["content"], 2000),
                    "tool_calls": [
                        {
                            "id": call.get("id"),
                            "name": (call.get("function") or {}).get("name") if isinstance(call, dict) else None,
                            "arguments": (call.get("function") or {}).get("arguments") if isinstance(call, dict) else None,
                        }
                        for call in tool_calls
                        if isinstance(call, dict)
                    ],
                }
            )

            if not tool_calls:
                completed = True
                final_message = assistant_entry["content"] or None
                break

            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                name = function.get("name")
                raw_arguments = function.get("arguments")
                try:
                    args = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                except json.JSONDecodeError:
                    args = None
                ok, output = self._execute_tool(name, args, workspace, deadline - time.monotonic())
                events.append(
                    {
                        "type": "tool.result",
                        "turn": turns,
                        "tool": name,
                        "ok": ok,
                        "output": _truncate(output, 2000),
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or f"call_{turns}_{len(messages)}",
                        "content": output,
                    }
                )
        else:
            error_text = f"exceeded {MAX_TURNS} model turns without a final message"

        duration = time.monotonic() - start
        if "total_tokens" not in usage and "prompt_tokens" in usage and "completion_tokens" in usage:
            usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        success = completed and not timed_out and error_text is None and bool(requested_model)
        stderr_parts = [part for part in (error_text, "no final message received" if timed_out and not error_text else None) if part]

        return AgentRunResult(
            agent=self.name,
            model=requested_model,
            workspace=workspace,
            duration_s=duration,
            success=success,
            returncode=0 if completed else None,
            command=recorded_command,
            stdout="",
            stderr="\n".join(stderr_parts),
            version=None,
            events=events,
            usage=usage,
            cost_usd=cost_usd,
            final_message=final_message,
            permissions={
                "sandbox": "workspace-write",
                "network": True,
                "session_persistence": False,
            },
            metadata={
                "base_url": base_url,
                "requested_model": requested_model,
                "models_observed": sorted(models_observed),
                "model_source": "requested" if model else "env:OPENAI_COMPATIBLE_MODEL",
                "requested_seed": seed,
                "timed_out": timed_out,
                "timeout_s": timeout_s,
                "turns": turns,
                "max_turns": MAX_TURNS,
                "structured_completion": completed,
                "cost_available": cost_usd is not None,
                "cost_is_estimate": False,
                "api_key_source": "env:OPENAI_COMPATIBLE_API_KEY",
            },
        )
