"""
명령어 사용, 이모지 확대, 서버 참가와 퇴장 기록. 대시보드용
"""
import os
import sqlite3
import time
from pathlib import Path

from utils.logging_config import get_logger

logger = get_logger('기록')

# 테스트는 MONGSIL_DB=:memory:로 파일을 만들지 않음
DB_PATH = os.getenv('MONGSIL_DB') or './data/dashboard.db'

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

CREATE TABLE IF NOT EXISTS zoom_log (
    ts INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS zoom_log_ts ON zoom_log (ts);
CREATE INDEX IF NOT EXISTS zoom_log_guild ON zoom_log (guild_id, ts);

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
if DB_PATH != ':memory:':
    Path(DB_PATH).parent.mkdir(exist_ok=True)
db = sqlite3.connect(DB_PATH, isolation_level=None)
db.row_factory = sqlite3.Row
db.executescript(SCHEMA)
# 결과가 화면에 표시된 시점. ms는 명령 처리가 모두 끝난 시점이라 체감 속도와 다름
# recv_ms는 봇이 명령을 받은 시점, enter_ms는 카드 생성 시작, loading_ms는 로딩 카드 전송 완료, built_ms는 카드 준비 완료.
# api_ms와 sem_ms는 카드 생성 중 API 요청 시간과 슬롯 대기 시간의 합. 지연 구간을 나누는 용도
TIMING_COLS = ('shown_ms', 'recv_ms', 'built_ms', 'enter_ms', 'loading_ms', 'api_ms', 'sem_ms')
_cols = {r[1] for r in db.execute("PRAGMA table_info(command_log)")}
for _col in TIMING_COLS:
    if _col not in _cols:
        db.execute(f"ALTER TABLE command_log ADD COLUMN {_col} INTEGER")


# 개인정보 처리방침에 적은 보관 기간
KEEP_SECONDS = 365 * 86400


def prune() -> None:
    """보관 기간이 지난 기록과 만료된 로그인 세션 삭제"""
    now = int(time.time())
    try:
        removed = 0
        for table in ('command_log', 'zoom_log', 'guild_events', 'settings_log'):
            removed += db.execute(f"DELETE FROM {table} WHERE ts < ?", (now - KEEP_SECONDS,)).rowcount
        db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        if removed:
            logger.info(f"보관 기간 지난 기록 {removed}건 삭제")
    except sqlite3.Error as e:
        logger.warning(f"오래된 기록 삭제 실패: {e}")


def record_command(guild_id, user_id: int, command: str, status: str, ms: int, shown_ms=None,
                   recv_ms=None, built_ms=None, enter_ms=None, loading_ms=None, api_ms=None, sem_ms=None) -> None:
    timing = (shown_ms, recv_ms, built_ms, enter_ms, loading_ms, api_ms, sem_ms)
    try:
        db.execute(
            f"INSERT INTO command_log (ts, guild_id, user_id, command, status, ms, {', '.join(TIMING_COLS)})"
            f" VALUES (?, ?, ?, ?, ?, ?{', ?' * len(TIMING_COLS)})",
            (int(time.time()), guild_id, user_id, command, status, ms, *timing),
        )
    except sqlite3.Error as e:
        logger.warning(f"명령어 기록 실패: {e}")


def record_zoom(guild_id: int, user_id: int) -> None:
    try:
        db.execute("INSERT INTO zoom_log VALUES (?, ?, ?)", (int(time.time()), guild_id, user_id))
    except sqlite3.Error as e:
        logger.warning(f"이모지 확대 기록 실패: {e}")


def zoom_count(guild_id: int, days: int = 30) -> int:
    try:
        return db.execute("SELECT COUNT(*) FROM zoom_log WHERE guild_id = ? AND ts >= ?",
                          (guild_id, int(time.time()) - days * 86400)).fetchone()[0]
    except sqlite3.Error:
        return 0


def recent_zoom_guilds(days: int) -> set:
    try:
        rows = db.execute("SELECT DISTINCT guild_id FROM zoom_log WHERE ts >= ?", (int(time.time()) - days * 86400,))
        return {r[0] for r in rows}
    except sqlite3.Error:
        return set()


def record_guild_event(guild_id: int, name: str, members, kind: str) -> None:
    try:
        db.execute(
            "INSERT INTO guild_events VALUES (?, ?, ?, ?, ?)",
            (int(time.time()), guild_id, name, members, kind),
        )
    except sqlite3.Error as e:
        logger.warning(f"서버 이벤트 기록 실패: {e}")
