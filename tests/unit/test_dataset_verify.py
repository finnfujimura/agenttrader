from pathlib import Path

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
    assert f"Using dataset path: {local_data}" in result.output
    assert "Dataset OK. Ready for backtesting." in result.output


def test_dataset_verify_falls_back_and_counts_recursive(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fallback_data = tmp_path / "fallback-data"
    _seed_complete_dataset(fallback_data)

    monkeypatch.setattr(dataset_cli, "DATA_DIR", fallback_data)

    runner = CliRunner()
    result = runner.invoke(dataset_cli.dataset_verify_cmd)
    assert result.exit_code == 0
    assert f"Using dataset path: {fallback_data}" in result.output
    # Recursive counts should find nested parquet files
    assert "polymarket/markets" in result.output
    assert "(1 parquet files)" in result.output
    assert "Dataset OK. Ready for backtesting." in result.output
