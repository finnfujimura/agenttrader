import importlib
import json
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner


def test_config_supports_split_state_and_data_roots(monkeypatch, tmp_path):
    config = importlib.import_module("agenttrader.config")
    db_mod = importlib.import_module("agenttrader.db")
    index_adapter = importlib.import_module("agenttrader.data.index_adapter")
    parquet_adapter = importlib.import_module("agenttrader.data.parquet_adapter")
    orderbook_store = importlib.import_module("agenttrader.data.orderbook_store")
    backtest_artifacts = importlib.import_module("agenttrader.data.backtest_artifacts")

    state_root = tmp_path / "agent-state"
    data_root = tmp_path / "agent-shared"

    try:
        with monkeypatch.context() as ctx:
            ctx.setenv("AGENTTRADER_STATE_DIR", str(state_root))
            ctx.setenv("AGENTTRADER_DATA_ROOT", str(data_root))

            config = importlib.reload(config)
            db_mod = importlib.reload(db_mod)
            index_adapter = importlib.reload(index_adapter)
            parquet_adapter = importlib.reload(parquet_adapter)
            orderbook_store = importlib.reload(orderbook_store)
            backtest_artifacts = importlib.reload(backtest_artifacts)

            assert config.STATE_DIR == state_root
            assert config.APP_DIR == state_root / ".agenttrader"
            assert config.DB_PATH == state_root / "db" / "db.sqlite"
            assert config.ORDERBOOK_DIR == state_root / ".agenttrader" / "orderbooks"
            assert config.ARTIFACTS_DIR == state_root / ".agenttrader" / "backtest_artifacts"
            assert config.DATA_ROOT == data_root
            assert config.SHARED_DATA_DIR == data_root / "data"
            assert config.BACKTEST_INDEX_PATH == data_root / "backtest_index.duckdb"

            engine = db_mod.get_engine()
            assert engine.url.database == str(config.DB_PATH)
            assert index_adapter.INDEX_PATH == config.BACKTEST_INDEX_PATH
            assert parquet_adapter.DATA_DIR == config.SHARED_DATA_DIR
            assert orderbook_store.OrderBookStore().base_path == config.ORDERBOOK_DIR
            assert backtest_artifacts.ARTIFACTS_DIR == config.ARTIFACTS_DIR
    finally:
        importlib.reload(config)
        importlib.reload(db_mod)
        importlib.reload(index_adapter)
        importlib.reload(parquet_adapter)
        importlib.reload(orderbook_store)
        importlib.reload(backtest_artifacts)


def test_config_defaults_shared_data_root_to_state_root(monkeypatch, tmp_path):
    config = importlib.import_module("agenttrader.config")

    state_root = tmp_path / "agent-state-only"

    try:
        with monkeypatch.context() as ctx:
            ctx.setenv("AGENTTRADER_STATE_DIR", str(state_root))
            ctx.delenv("AGENTTRADER_DATA_ROOT", raising=False)
            config = importlib.reload(config)

            assert config.STATE_DIR == state_root
            assert config.APP_DIR == state_root / ".agenttrader"
            assert config.DB_PATH == state_root / "db" / "db.sqlite"
            assert config.DATA_ROOT == config.DEFAULT_SHARED_DATA_ROOT
            assert config.SHARED_DATA_DIR == config.DEFAULT_SHARED_DATA_ROOT / "data"
            assert config.BACKTEST_INDEX_PATH == config.DEFAULT_SHARED_DATA_ROOT / "backtest_index.duckdb"
    finally:
        importlib.reload(config)


def test_init_local_state_writes_project_paths_file(monkeypatch, tmp_path):
    config = importlib.import_module("agenttrader.config")
    cli_config = importlib.import_module("agenttrader.cli.config")
    shared_root = tmp_path / "shared-data"

    with monkeypatch.context() as ctx:
        ctx.delenv("AGENTTRADER_STATE_DIR", raising=False)
        ctx.delenv("AGENTTRADER_DATA_ROOT", raising=False)
        ctx.chdir(tmp_path)
        config = importlib.reload(config)
        cli_config = importlib.reload(cli_config)
        ctx.setattr(cli_config.alembic_command, "upgrade", lambda *_args, **_kwargs: None)
        ctx.setattr(cli_config.sys, "stdin", SimpleNamespace(isatty=lambda: False))

        result = CliRunner().invoke(cli_config.init_cmd, ["--local-state", "--data-root", str(shared_root)])

        assert result.exit_code == 0
        project_file = tmp_path / ".agenttrader-paths.json"
        project_paths = json.loads(project_file.read_text(encoding="utf-8"))
        state_dir = tmp_path.resolve()
        app_dir = state_dir / ".agenttrader"

        assert project_file.exists()
        assert Path(project_paths["state_dir"]) == state_dir
        assert Path(project_paths["data_root"]) == shared_root.resolve()
        assert (state_dir / "db" / "db.sqlite").exists()
        assert (app_dir / "config.yaml").exists()
        assert f"Project path config: {project_file}" in result.output
        assert f"Initialized {app_dir}/" in result.output
        assert f"Shared data root: {shared_root.resolve()}" in result.output

    importlib.reload(config)
    importlib.reload(cli_config)


def test_init_defaults_to_project_local_state(monkeypatch, tmp_path):
    config = importlib.import_module("agenttrader.config")
    cli_config = importlib.import_module("agenttrader.cli.config")

    with monkeypatch.context() as ctx:
        ctx.delenv("AGENTTRADER_STATE_DIR", raising=False)
        ctx.delenv("AGENTTRADER_DATA_ROOT", raising=False)
        ctx.chdir(tmp_path)
        config = importlib.reload(config)
        cli_config = importlib.reload(cli_config)
        ctx.setattr(cli_config.alembic_command, "upgrade", lambda *_args, **_kwargs: None)
        ctx.setattr(cli_config.sys, "stdin", SimpleNamespace(isatty=lambda: False))

        result = CliRunner().invoke(cli_config.init_cmd, [])

        assert result.exit_code == 0
        project_file = tmp_path / ".agenttrader-paths.json"
        project_paths = json.loads(project_file.read_text(encoding="utf-8"))
        state_dir = tmp_path.resolve()
        app_dir = state_dir / ".agenttrader"

        assert project_file.exists()
        assert Path(project_paths["state_dir"]) == state_dir
        assert Path(project_paths["data_root"]) == config.DEFAULT_SHARED_DATA_ROOT.resolve()
        assert (state_dir / "db" / "db.sqlite").exists()
        assert (app_dir / "config.yaml").exists()
        assert f"Project path config: {project_file}" in result.output
        assert f"Initialized {app_dir}/" in result.output
        assert f"Shared data root: {config.DEFAULT_SHARED_DATA_ROOT.resolve()}" in result.output

    importlib.reload(config)
    importlib.reload(cli_config)
