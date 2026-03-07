import importlib
import itertools
import os
from pathlib import Path
import shutil
import tempfile

import pytest


_TEMP_ROOT = Path.cwd() / "codex_tmp_test_runtime"
_COUNTER = itertools.count()
mcp_server = importlib.import_module("agenttrader.mcp.server")


def _make_temp_dir(prefix: str = "tmp", suffix: str = "", dir: str | None = None) -> str:
    base_dir = Path(dir) if dir else _TEMP_ROOT
    base_dir.mkdir(parents=True, exist_ok=True)
    while True:
        idx = next(_COUNTER)
        path = base_dir / f"{prefix}{idx}{suffix}"
        if path.exists():
            continue
        path.mkdir(parents=True, exist_ok=False)
        return str(path)


class _StableTemporaryDirectory:
    def __init__(self, suffix: str | None = None, prefix: str | None = None, dir: str | None = None):
        self.name = _make_temp_dir(prefix or "tmp", suffix or "", dir)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.cleanup()
        return False

    def cleanup(self) -> None:
        shutil.rmtree(self.name, ignore_errors=True)


@pytest.fixture(autouse=True)
def _stable_tempfile(monkeypatch):
    _TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tempfile, "mkdtemp", _make_temp_dir)
    monkeypatch.setattr(tempfile, "TemporaryDirectory", _StableTemporaryDirectory)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(_TEMP_ROOT))
    monkeypatch.setattr(tempfile, "tempdir", str(_TEMP_ROOT), raising=False)


@pytest.fixture
def tmp_path() -> Path:
    path = Path(_make_temp_dir(prefix="pytest-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _stub_pmxt_sidecar_scan(monkeypatch):
    monkeypatch.setattr(mcp_server, "_list_process_command_lines", lambda: [])


def pytest_collection_modifyitems(config, items):
    run_live = os.getenv("AGENTTRADER_RUN_LIVE_TESTS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if run_live:
        return

    skip_live = pytest.mark.skip(
        reason="live tests disabled; set AGENTTRADER_RUN_LIVE_TESTS=1 to enable"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
