"""The committed docs/api/openapi.json must match the live app (audit DOC-2).

Regenerate with: python tools/export_openapi.py
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "docs" / "api" / "openapi.json"
HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}


def _exporter():
    spec = importlib.util.spec_from_file_location("export_openapi", ROOT / "tools" / "export_openapi.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _operations(spec: dict) -> set[tuple[str, str]]:
    return {(method.upper(), path) for path, item in spec["paths"].items() for method in item
            if method in HTTP_METHODS}


def test_committed_spec_lists_every_live_operation():
    live = _exporter().build_spec()
    committed = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    missing = sorted(_operations(live) - _operations(committed))
    stale = sorted(_operations(committed) - _operations(live))
    assert not missing and not stale, (
        f"docs/api/openapi.json is out of date (missing {missing}, stale {stale}); "
        "run: python tools/export_openapi.py")
    # Schema bodies are not compared byte for byte: they vary with the installed FastAPI and
    # Pydantic versions, which use open ranges. `tools/export_openapi.py --check` does the exact check.
