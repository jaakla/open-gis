from __future__ import annotations

import importlib.util
import io
import json
import shlex
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = REPO_ROOT / "evals" / "run.py"
SPEC = importlib.util.spec_from_file_location("open_gis_eval_runner", RUNNER_PATH)
assert SPEC and SPEC.loader
eval_runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = eval_runner
SPEC.loader.exec_module(eval_runner)

from adapters.base import AgentRunResult  # noqa: E402


class EvalRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="open-gis-eval-runner-test-")
        self.root = Path(self.tempdir.name)
        self.cases_dir = self.root / "cases"
        self.results_dir = self.root / "results"
        self.cases_dir.mkdir()
        self.patch_cases = patch.object(eval_runner, "CASES_DIR", self.cases_dir)
        self.patch_results = patch.object(eval_runner, "RESULTS_DIR", self.results_dir)
        self.patch_cases.start()
        self.patch_results.start()

    def tearDown(self) -> None:
        self.patch_results.stop()
        self.patch_cases.stop()
        self.tempdir.cleanup()

    def write_case(
        self,
        case_id: str = "test-case",
        *,
        modes: list[str] | None = None,
        score_types: dict[str, str] | None = None,
        generator: str | None = None,
        extra_generators: dict[str, str] | None = None,
        rerun_generator: str | None = None,
        expect: str = "passed",
        live_fixtures: list[dict[str, str]] | None = None,
        source_baseline: list[dict[str, str]] | None = None,
        extra_assertions: list[dict] | None = None,
    ) -> Path:
        modes = modes or ["fixture"]
        score_types = score_types or {
            mode: "agent_benchmark" if mode == "live" else "contract_ci" for mode in modes
        }
        case_dir = self.cases_dir / case_id
        project_dir = case_dir / "project"
        project_dir.mkdir(parents=True)
        (project_dir / "marker.txt").write_text("ok\n", encoding="utf-8")
        case = {
            "id": case_id,
            "case_type": (
                "mutation" if "mutation_tests" in score_types.values() else "positive"
            ),
            "modes": modes,
            "score_types": score_types,
            "project_dir": "project",
            "assertions": [
                {
                    "assert": "project.exists",
                    "args": {"path": "marker.txt"},
                    "expect": expect,
                }
            ]
            + (extra_assertions or []),
        }
        if "fixture" in modes:
            case["fixture"] = {}
            if generator is not None:
                case["fixture"]["generator"] = generator
            if extra_generators is not None:
                case["fixture"]["extra_generators"] = extra_generators
            if rerun_generator is not None:
                case["fixture"]["rerun_generator"] = rerun_generator
            if source_baseline is not None:
                case["fixture"]["source_baseline"] = source_baseline
        if "live" in modes:
            case["live"] = {
                "prompt_file": "prompt.md",
                "agent_workdir": "project",
                "fixtures": live_fixtures or [],
            }
        (case_dir / "expected.yaml").write_text(
            yaml.safe_dump(case, sort_keys=False), encoding="utf-8"
        )
        if "live" in modes:
            (case_dir / "prompt.md").write_text("Build the project.\n", encoding="utf-8")
        return case_dir

    def call_main(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = eval_runner.main(argv)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_fixture_mode_is_default_and_passes(self) -> None:
        self.write_case()
        exit_code, stdout, _ = self.call_main([])
        self.assertEqual(exit_code, 0, stdout)
        payload = json.loads((self.results_dir / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["run_config"]["mode"], "fixture")
        self.assertEqual(payload["schema"], "open-gis-eval-results/v2")
        self.assertEqual(payload["score_types"]["contract_ci"]["passed"], 1)

    def test_zero_live_cases_is_setup_error(self) -> None:
        self.write_case(modes=["fixture"])
        output = self.root / "live.json"
        exit_code, _, _ = self.call_main(["--mode", "live", "--json", str(output)])
        self.assertEqual(exit_code, 2)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["selection"]["trials_run"], 0)
        self.assertEqual(payload["selection"]["case_definitions_skipped"], 1)
        self.assertEqual(payload["setup_errors"][0]["stage"], "selection")

    def test_nonzero_generator_is_setup_failure_and_assertions_do_not_run(self) -> None:
        command = f'{shlex.quote(sys.executable)} -c "raise SystemExit(7)"'
        case_dir = self.write_case(generator=command, expect="failed")
        result = eval_runner.run_case(case_dir, "fixture")
        self.assertEqual(result["status"], "setup_failed")
        self.assertEqual(result["setup_error"]["stage"], "generator")
        self.assertEqual(result["generator"]["returncode"], 7)
        self.assertEqual(result["assertions"], [])
        self.assertIsNone(result["workspace"])
        self.assertEqual(result["generator"]["cwd"], "$WORKSPACE/project")
        self.assertNotIn("/tmp/open-gis-eval-", json.dumps(result))

    def test_assertion_mismatch_uses_exit_one_not_setup_exit(self) -> None:
        self.write_case(expect="failed")
        output = self.root / "assertion-failure.json"
        exit_code, _, _ = self.call_main(["--json", str(output)])
        self.assertEqual(exit_code, 1)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["outcomes"]["assertions_failed"], 1)
        self.assertEqual(payload["outcomes"]["setup_failed"], 0)
        self.assertEqual(payload["results"][0]["status"], "assertions_failed")

    def test_generator_timeout_is_setup_failure(self) -> None:
        command = f'{shlex.quote(sys.executable)} -c "import time; time.sleep(2)"'
        case_dir = self.write_case(generator=command)
        result = eval_runner.run_case(case_dir, "fixture", timeout_s=0.01)
        self.assertEqual(result["status"], "setup_failed")
        self.assertTrue(result["generator"]["timed_out"])
        self.assertIn("timed out", result["setup_error"]["message"])

    def test_extra_and_rerun_generator_failures_are_setup_failures(self) -> None:
        failure = f'{shlex.quote(sys.executable)} -c "raise SystemExit(8)"'
        cases = (
            self.write_case("extra-failure", extra_generators={"project_b": failure}),
            self.write_case("rerun-failure", rerun_generator=failure),
        )
        expected_stages = ("extra_generator:project_b", "rerun_generator")
        for case_dir, stage in zip(cases, expected_stages, strict=True):
            with self.subTest(stage=stage):
                result = eval_runner.run_case(case_dir, "fixture")
                self.assertEqual(result["status"], "setup_failed")
                self.assertEqual(result["setup_error"]["stage"], stage)

    def test_missing_agent_cli_is_preflight_setup_failure(self) -> None:
        case_dir = self.write_case("live-case", modes=["live"])

        class MissingAdapter:
            executable = "definitely-missing-agent"

            @staticmethod
            def is_available() -> bool:
                return False

        with patch.object(eval_runner, "_load_adapter", return_value=MissingAdapter()):
            result = eval_runner.run_case(case_dir, "live", agent_override="codex")
        self.assertEqual(result["status"], "setup_failed")
        self.assertEqual(result["setup_error"]["stage"], "agent_preflight")
        self.assertEqual(result["assertions"], [])

    def test_failed_agent_is_setup_failure_with_complete_evidence(self) -> None:
        case_dir = self.write_case("live-case", modes=["live"])

        class FailedAdapter:
            executable = "fake-agent"

            @staticmethod
            def is_available() -> bool:
                return True

            @staticmethod
            def run(prompt, workspace, fixture=None, timeout_s=900, model=None, seed=None):
                return AgentRunResult(
                    agent="fake",
                    model=model,
                    workspace=workspace,
                    duration_s=0.25,
                    success=False,
                    returncode=9,
                    command=["fake-agent", prompt],
                    stdout="partial output",
                    stderr="agent error",
                    metadata={"requested_seed": seed, "timeout_s": timeout_s},
                )

        with patch.object(eval_runner, "_load_adapter", return_value=FailedAdapter()):
            result = eval_runner.run_case(
                case_dir,
                "live",
                agent_override="codex",
                model="test-model",
                seed=42,
            )
        self.assertEqual(result["status"], "setup_failed")
        self.assertEqual(result["setup_error"]["stage"], "agent_execution")
        self.assertEqual(result["agent_run"]["returncode"], 9)
        self.assertEqual(result["agent_run"]["stdout"], "partial output")
        self.assertEqual(result["agent_run"]["stderr"], "agent error")
        self.assertEqual(result["agent_run"]["model"], "test-model")

    def test_assertion_exception_cannot_satisfy_expected_failure(self) -> None:
        case_dir = self.write_case(expect="failed")

        def raising_assertion(workspace, **args):
            raise RuntimeError("broken assertion implementation")

        with patch.object(eval_runner, "_resolve_assertion", return_value=("project", raising_assertion)):
            result = eval_runner.run_case(case_dir, "fixture")
        self.assertEqual(result["status"], "setup_failed")
        self.assertEqual(result["setup_error"]["stage"], "assertion_execution")

    def test_malformed_case_definition_returns_setup_exit_code(self) -> None:
        case_dir = self.cases_dir / "bad-case"
        case_dir.mkdir()
        (case_dir / "expected.yaml").write_text(
            "id: bad-case\ncase_type: positive\nmode: imaginary\nassertions: []\n",
            encoding="utf-8",
        )
        output = self.root / "bad.json"
        exit_code, _, stderr = self.call_main(["--json", str(output)])
        self.assertEqual(exit_code, 2)
        self.assertIn("Invalid eval configuration", stderr)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["setup_errors"][0]["stage"], "configuration")

    def test_unexpected_workspace_error_is_reported_not_raised(self) -> None:
        self.write_case()
        output = self.root / "workspace-error.json"
        with patch.object(eval_runner, "_prepare_workspace", side_effect=OSError("disk unavailable")):
            exit_code, _, _ = self.call_main(["--json", str(output)])
        self.assertEqual(exit_code, 2)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["outcomes"]["setup_failed"], 1)
        self.assertEqual(payload["results"][0]["setup_error"]["stage"], "runner")
        self.assertIn("disk unavailable", payload["results"][0]["setup_error"]["message"])

    def test_repetitions_and_seed_are_recorded_per_trial(self) -> None:
        self.write_case()
        output = self.root / "repetitions.json"
        exit_code, stdout, _ = self.call_main(
            ["--repetitions", "2", "--seed", "100", "--json", str(output)]
        )
        self.assertEqual(exit_code, 0, stdout)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["selection"]["trials_run"], 2)
        self.assertEqual([result["trial"] for result in payload["results"]], [1, 2])
        self.assertEqual([result["seed"] for result in payload["results"]], [100, 101])

    def test_score_types_are_reported_separately(self) -> None:
        self.write_case("contract", score_types={"fixture": "contract_ci"})
        self.write_case("mutation", score_types={"fixture": "mutation_tests"})
        output = self.root / "scores.json"
        exit_code, stdout, _ = self.call_main(["--json", str(output)])
        self.assertEqual(exit_code, 0, stdout)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["score_types"]["contract_ci"]["passed"], 1)
        self.assertEqual(payload["score_types"]["mutation_tests"]["passed"], 1)
        self.assertEqual(payload["score_types"]["agent_benchmark"]["trials_run"], 0)
        self.assertNotIn("cases_passed", payload)
        self.assertIn("contract_ci: 1/1", stdout)
        self.assertIn("mutation_tests: 1/1", stdout)

    def test_multi_mode_live_run_is_isolated_and_receives_declared_fixtures(self) -> None:
        fixtures_dir = self.root / "fixtures"
        fixtures_dir.mkdir()
        (fixtures_dir / "input.txt").write_text("fixture input\n", encoding="utf-8")
        case_dir = self.write_case(
            "multi-mode",
            modes=["fixture", "live"],
            live_fixtures=[{
                "source": "../../fixtures/input.txt",
                "destination": "project/data/source/input.txt",
            }],
        )

        class SuccessfulAdapter:
            executable = "fake-agent"

            @staticmethod
            def is_available() -> bool:
                return True

            @staticmethod
            def run(prompt, workspace, fixture=None, timeout_s=900, model=None, seed=None):
                # The committed reference marker must not leak into a live benchmark.
                if (workspace / "marker.txt").exists():
                    raise AssertionError("live workspace inherited the reference solution")
                if (workspace / "data/source/input.txt").read_text(encoding="utf-8") != "fixture input\n":
                    raise AssertionError("declared live fixture was not prepared")
                (workspace / "marker.txt").write_text("agent output\n", encoding="utf-8")
                return AgentRunResult(
                    agent="fake",
                    model=model,
                    workspace=workspace,
                    duration_s=0.1,
                    success=True,
                    returncode=0,
                    command=["fake-agent"],
                    stdout="done",
                    stderr="",
                    metadata={"requested_seed": seed, "timeout_s": timeout_s},
                )

        fixture_result = eval_runner.run_case(case_dir, "fixture")
        with patch.object(eval_runner, "_load_adapter", return_value=SuccessfulAdapter()):
            live_result = eval_runner.run_case(case_dir, "live", agent_override="codex")

        self.assertEqual(fixture_result["status"], "passed")
        self.assertEqual(fixture_result["case_type"], "positive")
        self.assertEqual(fixture_result["score_type"], "contract_ci")
        self.assertEqual(live_result["status"], "passed")
        self.assertEqual(live_result["score_type"], "agent_benchmark")
        self.assertEqual(live_result["live_fixtures"][0]["destination"], "$WORKSPACE/project/data/source/input.txt")

    def test_score_type_keys_must_match_modes(self) -> None:
        case_dir = self.write_case("bad-score-map")
        expected_path = case_dir / "expected.yaml"
        case = yaml.safe_load(expected_path.read_text(encoding="utf-8"))
        case["score_types"] = {}
        expected_path.write_text(yaml.safe_dump(case, sort_keys=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "score_types keys must exactly match modes"):
            eval_runner._load_case(case_dir)

    def test_mutation_case_cannot_contribute_to_contract_score(self) -> None:
        case_dir = self.write_case(
            "misclassified-mutation", score_types={"fixture": "mutation_tests"}
        )
        expected_path = case_dir / "expected.yaml"
        case = yaml.safe_load(expected_path.read_text(encoding="utf-8"))
        case["score_types"]["fixture"] = "contract_ci"
        expected_path.write_text(yaml.safe_dump(case, sort_keys=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "mutation cases must be fixture-only"):
            eval_runner._load_case(case_dir)

    def test_clean_rerun_requires_an_execution_assertion(self) -> None:
        case_dir = self.write_case("ungraded-clean-rerun")
        expected_path = case_dir / "expected.yaml"
        case = yaml.safe_load(expected_path.read_text(encoding="utf-8"))
        case["fixture"]["clean_rerun"] = {}
        expected_path.write_text(yaml.safe_dump(case, sort_keys=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "clean_rerun requires"):
            eval_runner._load_case(case_dir)

    def test_source_hashes_magic_value_detects_generator_mutating_its_own_source(self) -> None:
        fixtures_dir = self.root / "fixtures"
        fixtures_dir.mkdir()
        origin = fixtures_dir / "input.txt"
        origin.write_text("immutable content\n", encoding="utf-8")

        mutate_script = self.root / "mutate.py"
        mutate_script.write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "out = Path(sys.argv[1])\n"
            "out.mkdir(parents=True, exist_ok=True)\n"
            "target = out / 'data' / 'source' / 'input.txt'\n"
            "target.parent.mkdir(parents=True, exist_ok=True)\n"
            "target.write_text('mutated by generator\\n')\n",
            encoding="utf-8",
        )
        generator = f"{shlex.quote(sys.executable)} {shlex.quote(str(mutate_script))} {{project_dir}}"
        case_dir = self.write_case(
            "mutated-source-case",
            generator=generator,
            source_baseline=[{
                "source": "../../fixtures/input.txt",
                "destination": "data/source/input.txt",
            }],
            extra_assertions=[{
                "assert": "overrides.source_files_byte_identical",
                "args": {"hashes_before": "$SOURCE_HASHES", "paths": ["data/source/input.txt"]},
                "expect": "failed",
            }],
        )
        result = eval_runner.run_case(case_dir, "fixture")
        self.assertEqual(result["status"], "passed", result)
        mutation_assertion = next(
            a for a in result["assertions"] if a["assert"] == "overrides.source_files_byte_identical"
        )
        self.assertEqual(mutation_assertion["actual_status"], "failed")
        self.assertTrue(mutation_assertion["matched_expectation"])

    def test_source_hashes_baseline_passes_when_generator_leaves_source_untouched(self) -> None:
        fixtures_dir = self.root / "fixtures"
        fixtures_dir.mkdir()
        origin = fixtures_dir / "input.txt"
        origin.write_text("immutable content\n", encoding="utf-8")

        copy_script = self.root / "copy.py"
        copy_script.write_text(
            "import shutil, sys\n"
            "from pathlib import Path\n"
            "out = Path(sys.argv[1])\n"
            "(out / 'data' / 'source').mkdir(parents=True, exist_ok=True)\n"
            f"shutil.copyfile({str(origin)!r}, out / 'data' / 'source' / 'input.txt')\n",
            encoding="utf-8",
        )
        generator = f"{shlex.quote(sys.executable)} {shlex.quote(str(copy_script))} {{project_dir}}"
        case_dir = self.write_case(
            "clean-source-case",
            generator=generator,
            source_baseline=[{
                "source": "../../fixtures/input.txt",
                "destination": "data/source/input.txt",
            }],
            extra_assertions=[{
                "assert": "overrides.source_files_byte_identical",
                "args": {"hashes_before": "$SOURCE_HASHES", "paths": ["data/source/input.txt"]},
            }],
        )
        result = eval_runner.run_case(case_dir, "fixture")
        self.assertEqual(result["status"], "passed", result)


if __name__ == "__main__":
    unittest.main()
