"""Shared helpers for direct evals/assertions/*.py unit tests.

These tests call assertion functions directly against small hand-built
workspaces -- they do not run the eval runner or any generator. Every
public assertion should have positive, negative, and (where a real
external dependency like DuckDB Spatial or PyQGIS is involved)
unavailable-dependency coverage here.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
EVALS_DIR = REPO_ROOT / "evals"
for path in (str(REPO_ROOT), str(EVALS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)


def make_workspace() -> Path:
    return Path(tempfile.mkdtemp(prefix="open-gis-assertion-test-"))


def write_project(workspace: Path, project: dict[str, Any], project_dir: str = ".") -> Path:
    root = workspace / project_dir
    root.mkdir(parents=True, exist_ok=True)
    path = root / "project.yaml"
    path.write_text(yaml.safe_dump(project, sort_keys=False), encoding="utf-8")
    return path


def write_json(workspace: Path, relative: str, payload: dict[str, Any], project_dir: str = ".") -> Path:
    path = workspace / project_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def minimal_project(**overrides: Any) -> dict[str, Any]:
    project: dict[str, Any] = {
        "schema": "open-gis-project/v1",
        "project": {
            "id": "test-project",
            "title": "Test project",
            "question": "Does the assertion work?",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "status": "validated",
        },
        "interpretation": {
            "objective": "Verify assertion behavior.",
            "assumptions": [{"id": "A1", "statement": "N/A", "rationale": "test fixture"}],
        },
        "sources": {
            "test_source": {
                "provider": "test-provider",
                "dataset": "test-dataset",
                "source_url": "https://example.invalid/data",
                "access": {"method": "local", "retrieved_at": "2026-01-01T00:00:00Z"},
                "version": {"identifier": "v1", "published_at": "2026-01-01"},
                "selection": {"filter": "id = 1"},
                "license": {"name": "CC0-1.0", "url": "https://example.invalid/license"},
                "rationale": "test fixture source",
            }
        },
        "overrides": [],
        "processing": {
            "analysis_crs": "EPSG:3301",
            "storage_crs": "EPSG:4326",
            "steps": [
                {"id": "load", "operation": "read", "source": "test_source", "output": "raw"},
                {"id": "export", "operation": "export", "input": "raw", "output": "final"},
            ],
        },
        "outputs": {
            "final": {"path": "data/derived/final.json", "format": "GeoJSON", "generated_by": "export"}
        },
        "validation": {"required": ["geometry_valid"], "domain_checks": []},
        "presentation": {
            "intent": "report",
            "primary_view": "report",
            "layout": {"type": "report"},
            "map": {"engine_preference": "maplibre", "layer_groups": [], "layers": []},
            "provenance_ui": {"show_assumptions": True},
        },
        "warnings": [],
        "runtime": {
            "implementation": {"preferred_engine": "python", "pipeline": "pipeline.py"},
            "environment": {"python": "3.12"},
        },
    }
    project.update(overrides)
    return project
