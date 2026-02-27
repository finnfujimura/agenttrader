# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from __future__ import annotations

import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

import click

from agenttrader.config import APP_DIR, ensure_app_dir
from agenttrader.cli.utils import json_errors


DATA_DIR = APP_DIR / "data"
DOWNLOAD_URL = "https://s3.jbecker.dev/data.tar.zst"


def _expected_dataset_dirs(base_dir: Path) -> list[Path]:
    return [
        base_dir / "polymarket" / "markets",
        base_dir / "polymarket" / "trades",
        base_dir / "polymarket" / "blocks",
        base_dir / "kalshi" / "markets",
        base_dir / "kalshi" / "trades",
    ]


def _normalize_extracted_layout(base_dir: Path) -> None:
    nested = base_dir / "data"
    if nested.exists() and not (base_dir / "polymarket").exists():
        for child in nested.iterdir():
            shutil.move(str(child), str(base_dir / child.name))
        nested.rmdir()


def _extract_with_python(archive_path: Path, dest: Path) -> None:
    """Fallback extraction using Python zstandard."""
    try:
        import zstandard
    except ImportError as exc:  # pragma: no cover - optional path
        raise RuntimeError("Install zstandard to extract .zst archives: pip install 'agenttrader[dataset]'") from exc

    click.echo("Using Python zstandard for extraction...")
    with archive_path.open("rb") as fh:
        dctx = zstandard.ZstdDecompressor()
        with dctx.stream_reader(fh) as reader:
            with tarfile.open(fileobj=reader, mode="r|") as tar:
                tar.extractall(dest, filter="data")
    click.echo("Extraction complete.")


def download_dataset() -> None:
    """Download and extract the Jon Becker prediction market parquet dataset."""
    ensure_app_dir()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = DATA_DIR / "data.tar.zst"

    click.echo(f"\nDownloading to {DATA_DIR} ...")
    click.echo("This will take a while depending on your connection.\n")

    def reporthook(count: int, block_size: int, total_size: int) -> None:
        if total_size <= 0:
            return
        percent = min(int(count * block_size * 100 / total_size), 100)
        mb_done = count * block_size / 1024 / 1024
        mb_total = total_size / 1024 / 1024
        click.echo(f"\r  {percent}% ({mb_done:.0f} / {mb_total:.0f} MB)", nl=False)

    try:
        urllib.request.urlretrieve(DOWNLOAD_URL, archive_path, reporthook)
        click.echo("\n  Download complete.")
    except Exception as exc:
        click.echo(f"\nDownload failed: {exc}")
        click.echo("Try again with: agenttrader dataset download")
        return

    click.echo("Extracting...")
    try:
        result = subprocess.run(
            [
                "tar",
                "--use-compress-program=unzstd",
                "-xf",
                str(archive_path),
                "-C",
                str(DATA_DIR),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            _extract_with_python(archive_path, DATA_DIR)
        else:
            click.echo("Extraction complete.")
    except FileNotFoundError:
        _extract_with_python(archive_path, DATA_DIR)

    _normalize_extracted_layout(DATA_DIR)
    archive_path.unlink(missing_ok=True)
    click.echo(f"\nDataset ready at {DATA_DIR}")
    click.echo("Run 'agenttrader dataset verify' to confirm all files are present.")


@click.group("dataset")
def dataset_group() -> None:
    """Manage the parquet backtest dataset."""


@dataset_group.command("download")
@json_errors
def dataset_download_cmd() -> None:
    """Download and extract the Jon Becker parquet dataset."""
    download_dataset()


@dataset_group.command("verify")
@json_errors
def dataset_verify_cmd() -> None:
    """Verify expected parquet dataset folders are present."""
    expected = _expected_dataset_dirs(DATA_DIR)
    all_ok = True
    for path in expected:
        files = list(path.glob("*.parquet")) if path.exists() else []
        status = "✓" if files else "✗ MISSING"
        click.echo(f"  {status}  {path.relative_to(Path.home())} ({len(files)} parquet files)")
        if not files:
            all_ok = False
    if all_ok:
        click.echo("\nDataset OK. Ready for backtesting.")
    else:
        click.echo("\nDataset incomplete. Run: agenttrader dataset download")
