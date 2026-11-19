"""
labs 정각 회차(scripts/er/tier/hourly.mjs)가 쓴 이터컷 계산 결과와 아시아1 상위 1000명 목록.
순위표 정기 조회, 컷 기록, 휴면 확인, 이터컷 계산은 labs가 하고 몽실봇은 읽기만 함(장부 510)
"""
import json
import os
import time
from typing import Dict, List, Optional

# 닉네임 없는 cut.json은 labs 화면도 함께 읽는 labs 자료 폴더에, 닉네임이 든 top.json은 몽실봇 자료 폴더에 있음
CUT = os.environ.get('LABS_CUT', '../_data/labs/er-cut/cut.json')
TOP = os.environ.get('LABS_TOP', 'data/labs/top.json')
# 시즌 초 8,300 도달 순서(장부 528). 공식 순위표에 오른 닉네임만 들어 있어 cut.json 옆에 둠
RACE = os.environ.get('LABS_RACE', '../_data/labs/er-cut/race.json')
# 기준 시각이 이보다 오래되면 카드에 자료가 늦어지고 있다고 적음
STALE = 3 * 3600

_cache: Dict[str, tuple] = {}


def _read(path: str) -> Optional[dict]:
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return hit[1] if hit else None
    _cache[path] = (mtime, data)
    return data


def raw() -> Optional[dict]:
    """/api/cut이 그대로 내보내는 cut.json"""
    return _read(CUT)


def cut(season_id: Optional[int]) -> Optional[dict]:
    data = _read(CUT)
    return data if data and season_id and data.get('season', {}).get('id') == season_id else None


def top(season_id: Optional[int]) -> Optional[List[dict]]:
    data = _read(TOP)
    return data.get('topRanks') if data and season_id and data.get('season') == season_id else None


def stale(data: dict, now: Optional[float] = None) -> bool:
    return (now or time.time()) - data.get('updatedAt', 0) > STALE


def race(season_id: Optional[int], now: Optional[float] = None) -> Optional[dict]:
    """순위표 300위가 8,300을 넘기 전까지 이터니티 줄 대신 보여 줄 race.json. 닫혔거나 늦어졌으면 None"""
    data = _read(RACE)
    if not data or not data.get('open') or not season_id or data.get('season') != season_id or stale(data, now):
        return None
    return data
