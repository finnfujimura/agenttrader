# DO NOT import dome_api_sdk here. Use agenttrader.data.dome_client only.
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def get_engine(db_path: Path = None):
    if db_path is None:
        db_path = Path.home() / ".agenttrader" / "db.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{db_path}", echo=False)


def get_session(engine):
    session_cls = sessionmaker(bind=engine)
    return session_cls()
