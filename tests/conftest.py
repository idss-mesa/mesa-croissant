"""Shared helpers for the mesa-croissant tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
UPSTREAM = FIXTURES / "upstream"
IMPORTS = FIXTURES / "import"


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def golden_paths() -> list[Path]:
    return sorted(FIXTURES.glob("golden_*.json"))


def upstream_paths() -> list[Path]:
    return sorted(UPSTREAM.glob("*.json"))


def import_paths() -> list[Path]:
    return sorted(IMPORTS.glob("*.json"))


@pytest.fixture(params=golden_paths(), ids=lambda p: p.stem)
def golden(request) -> dict:
    return load_json(request.param)


def pytest_report_header(config) -> str:
    try:
        from importlib.metadata import version

        return f"mlcroissant: {version('mlcroissant')}"
    except Exception:  # noqa: BLE001 - optional dependency
        return "mlcroissant: not installed (compatibility tests skipped)"
