#!/usr/bin/env python3
"""Open-GIS eval runner.

    python evals/run.py                       # every fixture case
    python evals/run.py --case attribute-override
    python evals/run.py --mode fixture
    python evals/run.py --mode live --agent claude_code
    python evals/run.py --json eval-results.json
    python evals/run.py --list

Fixture-mode cases run with no network access and no LLM account; they are
safe for every-PR CI. Live-mode cases invoke an AgentAdapter and must be run
manually/on a schedule — never required for ordinary PR merges.

Each case is a directory under evals/cases/<id>/ with an `expected.yaml`
declaring assertions to run against a workspace copy of `project/` (fixture
mode) or an agent-produced project (live mode). Every assertion entry may
declare `expect: passed|failed|warning|not_testable` (default `passed`) —
adversarial/negative cases prove the suite *catches* a flaw by expecting
`failed`, not by expecting the flaw to be absent.
"""

from __future__ import annotations

import argparse
import importlib
import json
import platform
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

EVALS_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVALS_DIR.parent
CASES_DIR = EVALS_DIR / "cases"
RESULTS_DIR = EVALS_DIR / "results"

sys.path.insert(0, str(EVALS_DIR))

from assertions import AssertionResult  # noqa: E402

# Rollup dimension per assertion module. A case's assertions land in one of
# these seven buckets so a benchmark never reduces to one misleading score.
DIMENSIONS = {
    "project": "reproducibility_compliance",
    "overrides": "override_handling",
    "provenance": "provenance",
    "geodata": "gis_correctness",
    "validation": "validation_integrity",
    "qgis": "presentation_contract",
    "presentation": "presentation_contract",
    "rerun": "rerun_success",
}


def _resolve_assertion(name: str):
    module_name, _, fn_name = name.partition(".")
    if not fn_name:
        raise ValueError(f"assertion name must be '<module>.<function>', got {name!r}")
    module = importlib.import_module(f"assertions.{module_name}")
    fn = getattr(module, fn_name, None)
    if fn is None:
        raise ValueError(f"assertions.{module_name} has no function {fn_name!r}")
    return module_name, fn


def _load_case(case_dir: Path) -> dict[str, Any]:
    expected_path = case_dir / "expected.yaml"
    with expected_path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _prepare_workspace(case_dir: Path, case_def: dict[str, Any]) -> Path:
    workspace = Path(tempfile.mkdtemp(prefix=f"open-gis-eval-{case_dir.name}-"))
    project_dirs = {case_def.get("project_dir", "project")}
    project_dirs.update((case_def.get("extra_generators") or {}).keys())
    for project_dir_name in project_dirs:
        project_src = case_dir / project_dir_name
        if project_src.exists():
            shutil.copytree(project_src, workspace / project_dir_name, dirs_exist_ok=True)
        else:
            (workspace / project_dir_name).mkdir(parents=True, exist_ok=True)
    return workspace


def _run_generator(case_dir: Path, case_def: dict[str, Any], workspace: Path) -> dict[str, Any] | None:
    """Optionally re-run a case's pipeline to prove rerun/reproducibility.

    `generator` in expected.yaml is a relative command run with cwd set to
    the workspace's project directory, e.g. `python pipeline.py`.
    """
    generator = case_def.get("generator")
    if not generator:
        return None
    import subprocess

    project_path = workspace / case_def.get("project_dir", "project")
    generator = generator.format(repo_root=REPO_ROOT, evals_dir=EVALS_DIR, project_dir=project_path)
    start = time.monotonic()
    proc = subprocess.run(
        generator, shell=True, cwd=project_path, capture_output=True, text=True, timeout=300,
    )
    duration = time.monotonic() - start
    return {
        "command": generator,
        "returncode": proc.returncode,
        "duration_s": duration,
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
    }


def run_case(case_dir: Path, mode_filter: str | None) -> dict[str, Any]:
    case_def = _load_case(case_dir)
    case_id = case_def.get("id", case_dir.name)
    case_mode = case_def.get("mode", "fixture")

    if mode_filter and case_mode != mode_filter:
        return {"id": case_id, "skipped": True, "reason": f"mode={case_mode} != --mode {mode_filter}"}

    started = time.monotonic()
    workspace = _prepare_workspace(case_dir, case_def)
    project_dir = case_def.get("project_dir", "project")

    generator_result = None
    try:
        if case_mode == "fixture":
            generator_result = _run_generator(case_dir, case_def, workspace)
            extra_generators = case_def.get("extra_generators") or {}
            for extra_dir_name, extra_cmd in extra_generators.items():
                import subprocess

                extra_path = workspace / extra_dir_name
                cmd = extra_cmd.format(repo_root=REPO_ROOT, evals_dir=EVALS_DIR, project_dir=extra_path)
                subprocess.run(cmd, shell=True, cwd=extra_path, capture_output=True, text=True, timeout=300)
        elif case_mode == "live":
            agent_name = case_def.get("agent", "claude_code")
            adapter_module = importlib.import_module(f"adapters.{agent_name}")
            adapter_cls_name = "".join(part.capitalize() for part in agent_name.split("_")) + "Adapter"
            adapter = getattr(adapter_module, adapter_cls_name)()
            prompt = (case_dir / case_def.get("prompt_file", "prompt.md")).read_text(encoding="utf-8")
            fixture_path = case_dir / case_def["fixture"] if case_def.get("fixture") else None
            agent_result = adapter.run(prompt, workspace / project_dir, fixture=fixture_path)
            generator_result = {
                "agent": agent_result.agent, "model": agent_result.model,
                "success": agent_result.success, "duration_s": agent_result.duration_s,
                "stderr_tail": agent_result.stderr[-4000:],
            }

        rerun_workspace_path: Path | None = None
        rerun_generator_cmd = case_def.get("rerun_generator")
        if rerun_generator_cmd:
            import subprocess

            rerun_workspace_path = Path(tempfile.mkdtemp(prefix=f"open-gis-eval-{case_dir.name}-rerun-"))
            cmd = rerun_generator_cmd.format(repo_root=REPO_ROOT, evals_dir=EVALS_DIR, project_dir=rerun_workspace_path)
            subprocess.run(cmd, shell=True, cwd=rerun_workspace_path, capture_output=True, text=True, timeout=300)

        assertion_results = []
        dimension_totals: dict[str, dict[str, int]] = {}

        for entry in case_def.get("assertions", []):
            assert_name = entry["assert"]
            args = dict(entry.get("args", {}) or {})
            if rerun_workspace_path is not None and args.get("rerun_workspace") == "$RERUN":
                args["rerun_workspace"] = str(rerun_workspace_path)
            expect = entry.get("expect", "passed")
            module_name, fn = _resolve_assertion(assert_name)

            try:
                result: AssertionResult = fn(workspace / project_dir, **args)
            except Exception as exc:  # noqa: BLE001
                result = AssertionResult("failed", f"assertion raised: {exc}")

            matched = result.status == expect
            dim = DIMENSIONS.get(module_name, "other")
            bucket = dimension_totals.setdefault(dim, {"passed": 0, "failed": 0, "warning": 0, "not_testable": 0})
            bucket["passed" if matched else "failed"] += 1 if matched else 0

            assertion_results.append({
                "assert": assert_name,
                "args": args,
                "expect": expect,
                "actual_status": result.status,
                "detail": result.detail,
                "matched_expectation": matched,
                "hard_gate": entry.get("hard_gate", case_def.get("hard_gate", True)),
                "data": result.data,
            })

        hard_failures = [a for a in assertion_results if a["hard_gate"] and not a["matched_expectation"]]

        return {
            "id": case_id,
            "mode": case_mode,
            "skipped": False,
            "duration_s": time.monotonic() - started,
            "workspace": str(workspace),
            "generator": generator_result,
            "assertions": assertion_results,
            "dimension_totals": dimension_totals,
            "status": "failed" if hard_failures else "passed",
            "hard_failures": [a["assert"] for a in hard_failures],
        }
    finally:
        keep = case_def.get("keep_workspace", False)
        if not keep:
            shutil.rmtree(workspace, ignore_errors=True)
            if rerun_workspace_path is not None:
                shutil.rmtree(rerun_workspace_path, ignore_errors=True)


def discover_cases(only: str | None) -> list[Path]:
    dirs = sorted(p for p in CASES_DIR.iterdir() if p.is_dir() and (p / "expected.yaml").exists())
    if only:
        dirs = [d for d in dirs if d.name == only or _load_case(d).get("id") == only]
    return dirs


def rollup_dimensions(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    rollup: dict[str, dict[str, int]] = {}
    for case in case_results:
        for dim, counts in (case.get("dimension_totals") or {}).items():
            bucket = rollup.setdefault(dim, {"passed": 0, "failed": 0})
            bucket["passed"] += counts.get("passed", 0)
            bucket["failed"] += counts.get("failed", 0)
    return rollup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", help="run only this case id/directory name")
    parser.add_argument("--mode", choices=["fixture", "live"], help="filter by case mode")
    parser.add_argument("--json", help="write full machine-readable results to this path")
    parser.add_argument("--list", action="store_true", help="list discovered cases and exit")
    args = parser.parse_args()

    case_dirs = discover_cases(args.case)
    if not case_dirs:
        print(f"No eval cases found (case={args.case!r})", file=sys.stderr)
        return 2

    if args.list:
        for d in case_dirs:
            case_def = _load_case(d)
            print(f"{case_def.get('id', d.name):40s} mode={case_def.get('mode', 'fixture')}")
        return 0

    mode_filter = args.mode
    results = []
    for case_dir in case_dirs:
        result = run_case(case_dir, mode_filter)
        results.append(result)
        if result.get("skipped"):
            print(f"SKIP  {result['id']:40s} ({result['reason']})")
            continue
        marker = "PASS" if result["status"] == "passed" else "FAIL"
        print(f"{marker}  {result['id']:40s} {result['duration_s']:.2f}s", end="")
        if result["hard_failures"]:
            print(f"  -- failed: {', '.join(result['hard_failures'])}")
        else:
            print()

    ran = [r for r in results if not r.get("skipped")]
    failed = [r for r in ran if r["status"] == "failed"]

    summary = {
        "schema": "open-gis-eval-results/v1",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "cases_total": len(results),
        "cases_run": len(ran),
        "cases_skipped": len(results) - len(ran),
        "cases_passed": len(ran) - len(failed),
        "cases_failed": len(failed),
        "dimensions": rollup_dimensions(ran),
        "results": results,
    }

    if args.json:
        out_path = Path(args.json)
        out_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        print(f"\nWrote {out_path}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "latest.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    print(f"\n{summary['cases_passed']}/{summary['cases_run']} cases passed "
          f"({summary['cases_skipped']} skipped)")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
