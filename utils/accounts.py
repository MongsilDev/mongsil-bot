"""
디스코드 유저별로 등록한 이터널 리턴 계정. 스팀 게임이라 본인 인증 없이 입력값을 믿음
"""
import sqlite3
import time
from typing import Optional, Tuple

from utils.logging_config import get_logger
from utils.usage_db import db

logger = get_logger('계정')

db.executescript("""
CREATE TABLE IF NOT EXISTS accounts (
    user_id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL,
    nickname TEXT NOT NULL,
    updated INTEGER NOT NULL
);
""")


def get(user_id: int) -> Optional[Tuple[str, str, int]]:
    """uid, 닉네임, 등록 시각"""
    try:
        row = db.execute("SELECT uid, nickname, updated FROM accounts WHERE user_id = ?", (user_id,)).fetchone()
    except sqlite3.Error as e:
        logger.warning(f"계정 조회 실패: {e}")
        return None
    return (row['uid'], row['nickname'], row['updated']) if row else None


def save(user_id: int, uid: str, nickname: str) -> bool:
    try:
        db.execute("INSERT OR REPLACE INTO accounts VALUES (?, ?, ?, ?)", (user_id, uid, nickname, int(time.time())))
        return True
    except sqlite3.Error as e:
        logger.warning(f"계정 저장 실패: {e}")
        return False


def rename(user_id: int, nickname: str) -> None:
    """게임 안에서 닉네임을 바꾼 경우 조회 결과로 갱신"""
    try:
        db.execute("UPDATE accounts SET nickname = ? WHERE user_id = ? AND nickname != ?", (nickname, user_id, nickname))
    except sqlite3.Error as e:
        logger.warning(f"계정 닉네임 갱신 실패: {e}")


def delete(user_id: int) -> None:
    try:
        db.execute("DELETE FROM accounts WHERE user_id = ?", (user_id,))
    except sqlite3.Error as e:
        logger.warning(f"계정 삭제 실패: {e}")
