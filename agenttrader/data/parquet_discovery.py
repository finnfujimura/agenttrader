from __future__ import annotations

from pathlib import Path


def discover_parquet_files(base_dir: Path) -> list[Path]:
    """Recursively discover parquet files, excluding hidden sidecars."""
    if not base_dir.exists():
        return []
    return sorted(
        path
        for path in base_dir.rglob("*.parquet")
        if not path.name.startswith(".")
    )


def discover_parquet_file_strings(base_dir: Path) -> list[str]:
    return [str(path) for path in discover_parquet_files(base_dir)]
