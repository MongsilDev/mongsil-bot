"""
서버별 설정과 변경 기록. 이모지 확대 켜고 끄기만 예전 settings.json에 남아 있음
"""
import json
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from utils.logging_config import get_logger
from utils.usage_db import db

logger = get_logger('서버설정')

db.executescript("""
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (guild_id, key)
);
CREATE TABLE IF NOT EXISTS settings_log (
    ts INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    user_id INTEGER,
    user_name TEXT,
    source TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS settings_log_guild ON settings_log (guild_id, ts);
CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
""")

DEFAULTS: Dict[str, Any] = {
    'zoom_mode': 'all',
    'zoom_channels': [],
    'command_mode': 'all',
    'command_channels': [],
    'invite_days': 0,
    'alert_patch': 0,
    'alert_maintenance': 0,
    'alert_cut': 0,
    'cut_hour': 9,
}

# 설정 키, 이름, 설명
ALERTS = [
    ('alert_patch', '패치 노트', '공식 패치 노트와 핫픽스'),
    ('alert_maintenance', '점검', '공식 점검 공지와 점검 완료'),
    ('alert_cut', '매일 이터컷', '아시아1 이터니티와 데미갓 컷'),
]
ALERT_NAMES = {key: name for key, name, _ in ALERTS}

# 메시지마다 읽는 값이 있어 전부 메모리에 올려 둠
_cache: Dict[int, Dict[str, Any]] = {}
for _row in db.execute("SELECT guild_id, key, value FROM guild_settings"):
    _cache.setdefault(_row['guild_id'], {})[_row['key']] = json.loads(_row['value'])


def get(guild_id: int, key: str) -> Any:
    return _cache.get(guild_id, {}).get(key, DEFAULTS[key])


def put(guild_id: int, key: str, value: Any) -> bool:
    try:
        if value == DEFAULTS[key]:
            db.execute("DELETE FROM guild_settings WHERE guild_id = ? AND key = ?", (guild_id, key))
            _cache.get(guild_id, {}).pop(key, None)
        else:
            db.execute("INSERT OR REPLACE INTO guild_settings VALUES (?, ?, ?)", (guild_id, key, json.dumps(value)))
            _cache.setdefault(guild_id, {})[key] = value
        return True
    except sqlite3.Error as e:
        logger.warning(f"서버 설정 저장 실패: {guild_id} {key} {e}")
        return False


def guilds_with(key: str) -> List[Tuple[int, Any]]:
    """기본값이 아닌 값을 둔 서버"""
    return [(gid, values[key]) for gid, values in _cache.items() if key in values]


def log(guild_id: int, text: str, user_id: Optional[int] = None, user_name: Optional[str] = None,
        source: str = 'dashboard') -> None:
    """source는 dashboard, discord, bot. bot은 권한이 없어 몽실봇이 스스로 끈 경우"""
    try:
        db.execute("INSERT INTO settings_log VALUES (?, ?, ?, ?, ?, ?)",
                   (int(time.time()), guild_id, user_id, user_name, source, text))
    except sqlite3.Error as e:
        logger.warning(f"설정 변경 기록 실패: {guild_id} {e}")


def history(guild_id: int, limit: int = 15) -> List[sqlite3.Row]:
    try:
        return db.execute("SELECT * FROM settings_log WHERE guild_id = ? ORDER BY ts DESC LIMIT ?",
                          (guild_id, limit)).fetchall()
    except sqlite3.Error:
        return []


def state(key: str, default: Any = None) -> Any:
    row = db.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
    return json.loads(row['value']) if row else default


def set_state(key: str, value: Any) -> bool:
    """알림 중복 방지가 이 저장에 기대므로 실패하면 False를 반환해 전송하지 않게 함"""
    try:
        db.execute("INSERT OR REPLACE INTO bot_state VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=False)))
        return True
    except sqlite3.Error as e:
        logger.error(f"상태 저장 실패: {key} {e}")
        return False


def zoom_allowed(guild_id: int, channel_id: int) -> bool:
    mode = get(guild_id, 'zoom_mode')
    if mode == 'all':
        return True
    return (channel_id in get(guild_id, 'zoom_channels')) == (mode == 'only')


def command_allowed(guild_id: int, channel_id: Optional[int]) -> bool:
    channels = get(guild_id, 'command_channels')
    return get(guild_id, 'command_mode') == 'all' or not channels or channel_id in channels
