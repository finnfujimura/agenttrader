"""Test that package-internal migrations include all required revisions."""
from pathlib import Path


def test_package_migrations_include_provenance():
    """0002_add_provenance must exist in package-internal migrations."""
    migrations_dir = Path(__file__).resolve().parent.parent.parent / "agenttrader" / "db" / "migrations" / "versions"
    files = [f.name for f in migrations_dir.glob("0002*")]
    assert len(files) == 1, f"Expected one 0002 migration, found: {files}"


def test_package_migrations_include_execution_mode():
    """0003_backtest_execution_mode must exist in package-internal migrations."""
    migrations_dir = Path(__file__).resolve().parent.parent.parent / "agenttrader" / "db" / "migrations" / "versions"
    files = [f.name for f in migrations_dir.glob("0003*")]
    assert len(files) == 1, f"Expected one 0003 migration, found: {files}"


def test_migration_chain_is_complete():
    """Migrations must form a complete chain: 0001 -> 0002 -> 0003."""
    migrations_dir = Path(__file__).resolve().parent.parent.parent / "agenttrader" / "db" / "migrations" / "versions"
    for prefix in ["0001", "0002", "0003"]:
        matches = list(migrations_dir.glob(f"{prefix}*"))
        assert len(matches) >= 1, f"Missing migration with prefix {prefix}"
