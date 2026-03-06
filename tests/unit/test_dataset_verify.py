import io
from pathlib import Path
from types import SimpleNamespace

import duckdb
from click.testing import CliRunner

from agenttrader.cli import dataset as dataset_cli


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def _seed_complete_dataset(base: Path) -> None:
    _touch(base / "polymarket" / "markets" / "partition=a" / "m1.parquet")
    _touch(base / "polymarket" / "trades" / "part-000.parquet")
    _touch(base / "polymarket" / "blocks" / "day=2024-01-01" / "b1.parquet")
    _touch(base / "kalshi" / "markets" / "part-000.parquet")
    _touch(base / "kalshi" / "trades" / "nested" / "t1.parquet")


def _write_small_parquet_dataset(base: Path) -> None:
    (base / "polymarket" / "markets").mkdir(parents=True, exist_ok=True)
    (base / "polymarket" / "trades").mkdir(parents=True, exist_ok=True)
    (base / "polymarket" / "blocks").mkdir(parents=True, exist_ok=True)
    (base / "kalshi" / "markets").mkdir(parents=True, exist_ok=True)
    (base / "kalshi" / "trades").mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect()
    conn.execute(
        f"""
        COPY (
            SELECT
                'pm-1'::VARCHAR AS id,
                'cond-1'::VARCHAR AS condition_id,
                'Will BTC close above $50k?'::VARCHAR AS question,
                'bitcoin-above-50k'::VARCHAR AS slug,
                '["Yes","No"]'::VARCHAR AS outcomes,
                '["0.45","0.55"]'::VARCHAR AS outcome_prices,
                '["yes-1","no-1"]'::VARCHAR AS clob_token_ids,
                12345.0::DOUBLE AS volume,
                FALSE AS closed,
                TIMESTAMP '2024-06-01 00:00:00' AS end_date,
                NOW()::TIMESTAMP AS _fetched_at
        ) TO '{base / "polymarket" / "markets" / "part-000.parquet"}' (FORMAT PARQUET)
        """
    )
    conn.execute(
        f"""
        COPY (
            SELECT
                1000::BIGINT AS block_number,
                '0xtx1'::VARCHAR AS transaction_hash,
                0::BIGINT AS log_index,
                '0xorder'::VARCHAR AS order_hash,
                '0xmaker'::VARCHAR AS maker,
                '0xtaker'::VARCHAR AS taker,
                'no-1'::VARCHAR AS maker_asset_id,
                'yes-1'::VARCHAR AS taker_asset_id,
                60::BIGINT AS maker_amount,
                40::BIGINT AS taker_amount,
                0::BIGINT AS fee,
                NULL::BIGINT AS timestamp,
                NOW()::TIMESTAMP AS _fetched_at,
                '0xcontract'::VARCHAR AS _contract
        ) TO '{base / "polymarket" / "trades" / "part-000.parquet"}' (FORMAT PARQUET)
        """
    )
    conn.execute(
        f"""
        COPY (
            SELECT
                1000::BIGINT AS block_number,
                '2024-01-01T00:00:00Z'::VARCHAR AS timestamp
        ) TO '{base / "polymarket" / "blocks" / "part-000.parquet"}' (FORMAT PARQUET)
        """
    )
    conn.execute(
        f"""
        COPY (
            SELECT
                'KXBTC-24JAN01-T50000'::VARCHAR AS ticker,
                'KXBTC'::VARCHAR AS event_ticker,
                'binary'::VARCHAR AS market_type,
                'Kalshi BTC market'::VARCHAR AS title,
                'finalized'::VARCHAR AS status,
                345600::BIGINT AS volume,
                'yes'::VARCHAR AS result,
                TIMESTAMP '2024-01-01 00:00:00' AS created_time,
                TIMESTAMP '2024-01-20 00:00:00' AS close_time,
                NOW()::TIMESTAMP AS _fetched_at
        ) TO '{base / "kalshi" / "markets" / "part-000.parquet"}' (FORMAT PARQUET)
        """
    )
    conn.execute(
        f"""
        COPY (
            SELECT
                'trade-1'::VARCHAR AS trade_id,
                'KXBTC-24JAN01-T50000'::VARCHAR AS ticker,
                12::BIGINT AS count,
                35::BIGINT AS yes_price,
                65::BIGINT AS no_price,
                'yes'::VARCHAR AS taker_side,
                TIMESTAMP '2024-01-03 00:00:00' AS created_time,
                NOW()::TIMESTAMP AS _fetched_at
        ) TO '{base / "kalshi" / "trades" / "part-000.parquet"}' (FORMAT PARQUET)
        """
    )
    conn.close()


def test_dataset_verify_prefers_local_data_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    local_data = tmp_path / "data"
    fallback_data = tmp_path / "fallback-data"
    _seed_complete_dataset(local_data)
    _seed_complete_dataset(fallback_data)

    monkeypatch.setattr(dataset_cli, "DATA_DIR", fallback_data)

    runner = CliRunner()
    result = runner.invoke(dataset_cli.dataset_verify_cmd)
    assert result.exit_code == 0
    # _pretty_path may strip the home directory prefix, so just check the
    # relative portion appears in the output.
    assert "data" in result.output
    assert "fallback-data" not in result.output
    assert "Dataset OK. Ready for backtesting." in result.output


def test_dataset_verify_falls_back_and_counts_recursive(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fallback_data = tmp_path / "fallback-data"
    _seed_complete_dataset(fallback_data)

    monkeypatch.setattr(dataset_cli, "DATA_DIR", fallback_data)

    runner = CliRunner()
    result = runner.invoke(dataset_cli.dataset_verify_cmd)
    assert result.exit_code == 0
    assert "fallback-data" in result.output
    # Recursive counts should find nested parquet files
    # Path separators may vary by OS
    assert "polymarket" in result.output and "markets" in result.output
    assert "(1 parquet files)" in result.output
    assert "Dataset OK. Ready for backtesting." in result.output


class _FakeResponse:
    def __init__(self, payload: bytes, *, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self._buffer = io.BytesIO(payload)
        self.status = status
        self.headers = headers or {"Content-Length": str(len(payload))}

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def test_download_archive_sets_user_agent_and_writes_file(tmp_path, monkeypatch):
    dest = tmp_path / "data.tar.zst"
    seen: dict[str, str | None] = {}

    def fake_urlopen(request):
        seen["user_agent"] = request.get_header("User-agent")
        seen["range"] = request.get_header("Range")
        return _FakeResponse(b"abc123")

    monkeypatch.setattr(dataset_cli.urllib.request, "urlopen", fake_urlopen)

    dataset_cli._download_archive("https://example.com/data.tar.zst", dest)

    assert dest.read_bytes() == b"abc123"
    assert seen["user_agent"] == dataset_cli.DOWNLOAD_USER_AGENT
    assert seen["range"] is None


def test_download_archive_resumes_partial_download(tmp_path, monkeypatch):
    dest = tmp_path / "data.tar.zst"
    partial = tmp_path / "data.tar.zst.part"
    partial.write_bytes(b"abc")
    seen: dict[str, str | None] = {}

    def fake_urlopen(request):
        seen["range"] = request.get_header("Range")
        return _FakeResponse(b"def", status=206, headers={"Content-Length": "3"})

    monkeypatch.setattr(dataset_cli.urllib.request, "urlopen", fake_urlopen)

    dataset_cli._download_archive("https://example.com/data.tar.zst", dest)

    assert dest.read_bytes() == b"abcdef"
    assert seen["range"] == "bytes=3-"
    assert not partial.exists()


def test_download_with_aria2_prefers_binary_and_promotes_partial(tmp_path, monkeypatch):
    dest = tmp_path / "data.tar.zst"
    partial = tmp_path / "data.tar.zst.part"
    partial.write_bytes(b"partial")
    seen: dict[str, object] = {}

    monkeypatch.setattr(dataset_cli.shutil, "which", lambda name: "/usr/local/bin/aria2c" if name == "aria2c" else None)

    def fake_run(cmd, check=False):
        seen["cmd"] = cmd
        seen["check"] = check
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(dataset_cli.subprocess, "run", fake_run)

    assert dataset_cli._download_with_aria2("https://example.com/data.tar.zst", dest) is True
    assert dest.read_bytes() == b"partial"
    assert not partial.exists()
    assert seen["check"] is False
    assert "--user-agent" in seen["cmd"]
    assert "--file-allocation=none" in seen["cmd"]
    assert dataset_cli.DOWNLOAD_USER_AGENT in seen["cmd"]


def test_download_dataset_falls_back_to_python_when_aria2_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(dataset_cli, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dataset_cli, "ensure_app_dir", lambda: None)
    monkeypatch.setattr(dataset_cli, "ensure_data_root", lambda: None)
    monkeypatch.setattr(dataset_cli, "_download_with_aria2", lambda url, dest: False)

    seen: dict[str, object] = {}

    def fake_download(url, dest):
        seen["download_url"] = url
        seen["download_dest"] = dest
        dest.write_bytes(b"archive")

    monkeypatch.setattr(dataset_cli, "_download_archive", fake_download)
    monkeypatch.setattr(dataset_cli.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(dataset_cli, "_normalize_extracted_layout", lambda base_dir: None)

    assert dataset_cli.download_dataset() is True
    assert seen["download_url"] == dataset_cli.DOWNLOAD_URL
    assert seen["download_dest"] == tmp_path / "data.tar.zst"
    assert not (tmp_path / "data.tar.zst").exists()


def test_dataset_build_index_cmd_accepts_data_dir(tmp_path):
    from agenttrader.data import index_builder as index_builder_mod

    source_dir = tmp_path / "external-data"
    _write_small_parquet_dataset(source_dir)
    index_path = tmp_path / "cmd-index.duckdb"

    dataset_cli.BACKTEST_INDEX_PATH = index_path
    index_builder_mod.INDEX_PATH = index_path

    runner = CliRunner()
    result = runner.invoke(dataset_cli.build_index_cmd, ["--data-dir", str(source_dir), "--force", "--json"])

    assert result.exit_code == 0
    assert '"ok": true' in result.output.lower()


def test_dataset_build_index_cmd_uses_default_raw_data_dir(tmp_path, monkeypatch):
    from agenttrader.data import index_builder as index_builder_mod

    raw_dir = tmp_path / "data"
    _write_small_parquet_dataset(raw_dir)
    index_path = tmp_path / "auto-index.duckdb"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dataset_cli, "DATA_DIR", raw_dir)
    monkeypatch.setattr(dataset_cli, "BACKTEST_INDEX_PATH", index_path)
    index_builder_mod.INDEX_PATH = index_path

    runner = CliRunner()
    result = runner.invoke(dataset_cli.build_index_cmd, ["--force", "--json"])

    assert result.exit_code == 0
    assert '"ok": true' in result.output.lower()
    assert '"compaction"' not in result.output.lower()
    assert sum(1 for _ in raw_dir.rglob("*.parquet")) == 5
