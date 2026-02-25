# DO NOT import dome_api_sdk here. Use agenttrader.data.dome_client only.
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_engine_cache: dict[str, object] = {}
_sessionmaker_cache: dict[int, sessionmaker] = {}


def get_engine(db_path: Path = None):
    if db_path is None:
        db_path = Path.home() / ".agenttrader" / "db.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    key = str(db_path)
    if key not in _engine_cache:
        _engine_cache[key] = create_engine(f"sqlite:///{db_path}", echo=False)
    return _engine_cache[key]


def get_session(engine):
    eid = id(engine)
    if eid not in _sessionmaker_cache:
        _sessionmaker_cache[eid] = sessionmaker(bind=engine)
    return _sessionmaker_cache[eid]()
