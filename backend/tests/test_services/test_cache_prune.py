"""Day-scoped disk cache pruning (``prune_dated_cache_files``).

DB-free: exercises the helper and its scanner/client call sites against
``tmp_path``. On Cloud Run the filesystem is in-memory, so stale cache
files cost RAM until something deletes them.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services.fmp_client import FMPClient, prune_dated_cache_files
from app.services.scanner import StockScanner


def _touch(dir_, name):
    p = dir_ / name
    p.write_text("{}")
    return p


def test_prunes_files_dated_before_today(tmp_path):
    today = date(2026, 10, 2)
    old = [
        _touch(tmp_path, "stage1_sp500_2026-10-01.json"),
        _touch(tmp_path, "hist_AAPL_2026-09-30.json"),
        _touch(tmp_path, "histfull_MSFT_2025-12-31.json"),  # any prefix
    ]
    keep = [
        _touch(tmp_path, "stage1_sp500_2026-10-02.json"),
        _touch(tmp_path, "hist_AAPL_2026-10-02.json"),
        _touch(tmp_path, "notes.json"),  # no date suffix
        _touch(tmp_path, "hist_AAPL_2026-09-30.txt"),  # not .json
    ]

    removed = prune_dated_cache_files(str(tmp_path), today=today)

    assert removed == 3
    assert not any(p.exists() for p in old)
    assert all(p.exists() for p in keep)


def test_prune_leaves_directories_alone(tmp_path):
    (tmp_path / "odd_2020-01-01.json").mkdir()
    assert prune_dated_cache_files(str(tmp_path), today=date(2026, 10, 2)) == 0
    assert (tmp_path / "odd_2020-01-01.json").is_dir()


def test_prune_missing_dir_is_noop(tmp_path):
    assert prune_dated_cache_files(str(tmp_path / "nope")) == 0


def test_client_and_scanner_construction_prune(tmp_path):
    stale = _touch(tmp_path, "hist_AAPL_2000-01-01.json")
    FMPClient(api_key="test", cache_dir=str(tmp_path))
    assert not stale.exists()

    client = FMPClient(api_key="test", cache_dir=str(tmp_path))
    stale = _touch(tmp_path, "stage1_custom_2000-01-01.json")
    StockScanner(client=client, cache_dir=str(tmp_path))
    assert not stale.exists()


@pytest.mark.asyncio
async def test_run_scan_prunes_at_start(tmp_path):
    client = FMPClient(api_key="test", cache_dir=str(tmp_path))
    scanner = StockScanner(client=client, cache_dir=str(tmp_path))
    # Simulate a warm instance that crossed midnight since construction.
    stale = _touch(tmp_path, "stage1_custom_2000-01-01.json")

    await scanner.run_scan([], use_cache=True)

    assert not stale.exists()
