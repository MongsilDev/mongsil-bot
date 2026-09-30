"""
명령어 사용과 서버 참가, 퇴장 기록. 대시보드용
"""
import sqlite3
import time
from pathlib import Path

from utils.logging_config import get_logger

logger = get_logger('기록')

DB_PATH = Path('./data/dashboard.db')

SCHEMA = """
CREATE TABLE IF NOT EXISTS command_log (
    ts INTEGER NOT NULL,
    guild_id INTEGER,
    user_id INTEGER NOT NULL,
    command TEXT NOT NULL,
    status TEXT NOT NULL,
    ms INTEGER
);
CREATE INDEX IF NOT EXISTS command_log_ts ON command_log (ts);
CREATE INDEX IF NOT EXISTS command_log_guild ON command_log (guild_id, ts);

CREATE TABLE IF NOT EXISTS guild_events (
    ts INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    name TEXT,
    members INTEGER,
    kind TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS guild_events_ts ON guild_events (ts);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    avatar TEXT,
    token TEXT NOT NULL,
    csrf TEXT NOT NULL,
    expires INTEGER NOT NULL
);
"""

# 쓰기가 명령 하나에 한 줄이라 이벤트 루프에서 직접 실행
DB_PATH.parent.mkdir(exist_ok=True)
db = sqlite3.connect(DB_PATH, isolation_level=None)
db.row_factory = sqlite3.Row
db.executescript(SCHEMA)


def record_command(guild_id, user_id: int, command: str, status: str, ms: int) -> None:
    try:
        db.execute(
            "INSERT INTO command_log VALUES (?, ?, ?, ?, ?, ?)",
            (int(time.time()), guild_id, user_id, command, status, ms),
        )
    except sqlite3.Error as e:
        logger.warning(f"명령어 기록 실패: {e}")


def record_guild_event(guild_id: int, name: str, members, kind: str) -> None:
    try:
        db.execute(
            "INSERT INTO guild_events VALUES (?, ?, ?, ?, ?)",
            (int(time.time()), guild_id, name, members, kind),
        )
    except sqlite3.Error as e:
        logger.warning(f"서버 이벤트 기록 실패: {e}")
