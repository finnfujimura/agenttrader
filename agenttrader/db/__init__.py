# DO NOT import pmxt here. Use agenttrader.data.pmxt_client only.
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

_engine_cache: dict[str, object] = {}
_sessionmaker_cache: dict[int, sessionmaker] = {}


def get_engine(db_path: Path = None):
    if db_path is None:
        db_path = Path.home() / ".agenttrader" / "db.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    key = str(db_path)
    if key not in _engine_cache:
        engine = create_engine(
            f"sqlite:///{db_path}",
            echo=False,
            connect_args={"timeout": 30},
        )

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        _engine_cache[key] = engine
    return _engine_cache[key]


def get_session(engine):
    eid = id(engine)
    if eid not in _sessionmaker_cache:
        _sessionmaker_cache[eid] = sessionmaker(bind=engine, expire_on_commit=False)
    return _sessionmaker_cache[eid]()
