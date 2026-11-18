"""
labs 정각 회차가 쓰는 아시아1 상위 순위와 이터컷의 시간별 기록. /랭킹 순위 변동과 /이터컷 추이용.
기록과 휴면 확인은 labs(scripts/er/tier/etercut.mjs)가 하고 몽실봇은 읽기만 함(장부 510)
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

PATH = os.environ.get('LABS_STATE', 'data/labs/rank_history.json')
KST = timezone(timedelta(hours=9))

_cache: Tuple[float, dict] = (0, {})


def _load() -> dict:
    """파일이 바뀌었을 때만 다시 읽음. 읽지 못하면 마지막으로 읽은 값"""
    global _cache
    try:
        mtime = os.stat(PATH).st_mtime
        if mtime != _cache[0]:
            with open(PATH, encoding='utf-8') as f:
                _cache = (mtime, json.load(f))
    except (OSError, ValueError):
        pass
    state = _cache[1]
    state.setdefault('ranks', [])
    state.setdefault('cuts', [])
    return state


def ranks_day_ago(season_id: int) -> Optional[Dict[str, int]]:
    """24시간 전에 가장 가까운 20~28시간 전 기록. 봇이 멈춰 그 사이 기록이 없으면 None"""
    now = time.time()
    old = [r for r in _load()['ranks'] if r[1] == season_id and 20 * 3600 <= now - r[0] <= 28 * 3600]
    if not old:
        return None
    return min(old, key=lambda r: abs(now - r[0] - 86400))[2]


def cut_history(season_id: int) -> List[tuple]:
    """(시각, 이터니티, 데미갓, 휴면 강등 이터니티, 휴면 강등 데미갓). 시즌 12는 10-04 16시부터 기록.
    그 전 09-01~10-04 오전 9시 순위표 값과 10-05~10-19 오전 9시 휴면 강등 값은 게임 기록으로 복원"""
    return [(c[0], c[2], c[3], *(c[4:6] + [None] * (6 - len(c)))) for c in _load()['cuts'] if c[1] == season_id]


def at_nine(history: List[tuple], index: int) -> List[Tuple[datetime, float]]:
    """날마다 KST 오전 9시에 가장 가까운 기록. 시즌이 오전 9시에 끝나서 같은 시각끼리 비교. 앞뒤 90분 안에 기록이 없는 날은 뺌"""
    days: Dict[datetime, Tuple[float, float]] = {}
    for h in history:
        if not h[index]:
            continue
        at = datetime.fromtimestamp(h[0], KST)
        nine = at.replace(hour=9, minute=0, second=0, microsecond=0)
        gap = abs((at - nine).total_seconds())
        if gap <= 5400 and (nine not in days or gap < days[nine][0]):
            days[nine] = (gap, h[index])
    return [(d, v) for d, (_, v) in sorted(days.items())]
