"""
아시아1 상위 순위와 이터컷의 시간별 기록. /랭킹 순위 변동과 /이터컷 추이용.
players는 상위 1000명의 마지막 RP 변화와 마지막 랭크 게임 시각으로, 휴면 계정 판별용
"""
import json
import os
import statistics
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from utils.logging_config import get_logger

logger = get_logger('순위기록')

PATH = 'data/rank_history.json'
INTERVAL = 3600
KEEP_RANKS = 49 * 3600
KEEP_CUTS = 120 * 86400
KEEP_PLAYERS = 14 * 86400
DORMANT_DAYS = 7
KST = timezone(timedelta(hours=9))
# players 항목 순서
MMR, CHANGED, FIRST, SEEN, LAST_GAME, CHECKED, UID = range(7)
# LAST_GAME 값. 0은 확인한 범위 안에 랭크 게임 없음, -1은 일반 게임만 많아 끝까지 못 봄
NO_RECENT, UNREADABLE = 0, -1

_state: Optional[dict] = None


def _load() -> dict:
    global _state
    if _state is None:
        try:
            with open(PATH, encoding='utf-8') as f:
                _state = json.load(f)
        except (OSError, ValueError):
            _state = {}
        _state.setdefault('ranks', [])
        _state.setdefault('cuts', [])
        _state.setdefault('players', {})
    return _state


def _save() -> None:
    tmp = f'{PATH}.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(_state, f, ensure_ascii=False)
        os.replace(tmp, PATH)
    except OSError as e:
        logger.warning(f"순위 기록 저장 실패: {e}")


def record_ranks(season_id: int, top_ranks: List[dict]) -> None:
    state = _load()
    now = time.time()
    ranks = [r for r in state['ranks'] if r[1] == season_id and now - r[0] < KEEP_RANKS]
    if ranks and now - ranks[-1][0] < INTERVAL:
        return
    ranks.append([now, season_id, {r['nickname']: r['rank'] for r in top_ranks if r.get('nickname')}])
    state['ranks'] = ranks
    _save()


def ranks_day_ago(season_id: int) -> Optional[Dict[str, int]]:
    """24시간 전에 가장 가까운 20~28시간 전 기록. 봇이 멈춰 그 사이 기록이 없으면 None"""
    now = time.time()
    old = [r for r in _load()['ranks'] if r[1] == season_id and 20 * 3600 <= now - r[0] <= 28 * 3600]
    if not old:
        return None
    return min(old, key=lambda r: abs(now - r[0] - 86400))[2]


def record_cuts(season_id: int, eternity: Optional[int], demigod: Optional[int],
                active_eternity: Optional[int] = None, active_demigod: Optional[int] = None) -> None:
    if not eternity and not demigod:
        return
    state = _load()
    now = time.time()
    cuts = [c for c in state['cuts'] if c[1] == season_id and now - c[0] < KEEP_CUTS]
    if cuts and now - cuts[-1][0] < INTERVAL:
        return
    cuts.append([now, season_id, eternity, demigod, active_eternity, active_demigod])
    state['cuts'] = cuts
    _save()


def cut_history(season_id: int) -> List[tuple]:
    """(시각, 이터니티, 데미갓, 휴면 제외 이터니티, 휴면 제외 데미갓). 휴면 제외 값은 10-19부터 기록"""
    return [(c[0], c[2], c[3], *(c[4:6] + [None] * (6 - len(c)))) for c in _load()['cuts'] if c[1] == season_id]


def record_players(season_id: int, top_ranks: List[dict]) -> None:
    """닉네임별 [RP, 마지막 변화, 처음 본 시각, 마지막으로 본 시각]. 닉네임을 바꾸면 새 계정으로 보임"""
    state = _load()
    now = time.time()
    book = state['players']
    if book.get('season') != season_id:
        book = state['players'] = {'season': season_id, 'at': 0, 'since': now, 'list': {}}
    if now - book['at'] < INTERVAL:
        return
    players = book['list']
    for r in top_ranks:
        name, mmr = r.get('nickname'), r.get('mmr')
        if not name or mmr is None:
            continue
        old = players.get(name)
        if old is None:
            players[name] = [int(mmr), now, now, now, None, None, None]
            continue
        old.extend([None] * (UID + 1 - len(old)))
        delta = int(mmr) - old[MMR]
        if delta:
            # 휴면 차감은 하루 100점. 게임으로 바뀐 RP만 활동으로 봄
            if not (delta < 0 and delta % 100 == 0):
                old[CHANGED] = now
            old[MMR] = int(mmr)
        old[SEEN] = now
    for name in [n for n, p in players.items() if now - p[3] > KEEP_PLAYERS]:
        del players[name]
    book['at'] = now
    _save()


def player_book(season_id: int) -> Dict[str, list]:
    book = _load()['players']
    if book.get('season') != season_id:
        return {}
    for p in book['list'].values():
        p.extend([None] * (UID + 1 - len(p)))
    return book['list']


def save() -> None:
    _save()


def last_active(p: list) -> Optional[float]:
    """확인된 마지막 활동 시각. 기록을 시작한 뒤 RP가 바뀐 적도 없고 API 확인도 안 했으면 None"""
    seen = [p[CHANGED]] if p[CHANGED] > p[FIRST] else []
    if len(p) > LAST_GAME and p[LAST_GAME] is not None:
        if p[LAST_GAME] == UNREADABLE:
            seen.append(p[CHECKED])
        elif p[LAST_GAME] > 0:
            seen.append(p[LAST_GAME])
        else:
            seen.append(0)
    return max(seen) if seen else None


def is_dormant(p: list, now: float) -> Optional[bool]:
    at = last_active(p)
    return None if at is None else now - at > DORMANT_DAYS * 86400


def active_cut(season_id: int, top_ranks: List[dict], place: int) -> Optional[Tuple[int, int, bool]]:
    """휴면 계정을 뺀 place번째 RP, 그 위에서 뺀 휴면 계정 수, 추정 여부. 판별 안 된 계정이 있으면 None.
    목록이 1000위까지라 활동 계정이 place명이 안 되면 끝부분 순위당 RP 감소폭과 휴면 비율로 나머지를 추정"""
    players = player_book(season_id)
    book = _load()['players']
    since = book.get('since') or min((p[FIRST] for p in players.values()), default=0)
    now = time.time()
    ranked = sorted((r for r in top_ranks if r.get('mmr')), key=lambda r: r.get('rank', 0))
    active = dormant = 0
    tail: List[bool] = []
    for r in ranked:
        p = players.get(r.get('nickname'))
        state = is_dormant(p, now) if p else None
        if state is None:
            # 기록을 시작한 뒤 새로 들어온 계정은 올라왔거나 닉네임을 바꾼 것이라 활동으로 봄
            if p and p[FIRST] - since < 3600:
                return None
            state = False
        tail.append(state)
        if state:
            dormant += 1
            continue
        active += 1
        if active == place:
            return int(r['mmr']), dormant, False
    if len(ranked) < 200:
        return None
    window = ranked[-100:]
    per_rank = (int(window[0]['mmr']) - int(window[-1]['mmr'])) / (len(window) - 1)
    active_share = 1 - sum(tail[-100:]) / 100
    if active_share <= 0:
        return None
    beyond = (place - active) / active_share
    return round(int(ranked[-1]['mmr']) - per_rank * beyond), dormant, True


def daily(history: List[tuple], index: int) -> List[Tuple[datetime, float]]:
    """KST 날짜별 중앙값. 6건 미만인 날은 뺌"""
    days: Dict[datetime, List[float]] = {}
    for h in history:
        if h[index]:
            day = datetime.fromtimestamp(h[0], KST).replace(hour=12, minute=0, second=0, microsecond=0)
            days.setdefault(day, []).append(h[index])
    return [(d, statistics.median(v)) for d, v in sorted(days.items()) if len(v) >= 6]


def _pair_slopes(points: List[Tuple[datetime, float]]) -> List[float]:
    return sorted((b[1] - a[1]) / ((b[0] - a[0]).total_seconds() / 86400)
                  for i, a in enumerate(points) for b in points[i + 1:])


def _theil_sen(points: List[Tuple[datetime, float]]) -> float:
    return statistics.median(_pair_slopes(points))


def forecast(points: List[Tuple[datetime, float]], start: float, now: datetime,
             end: datetime, min_days: int = 7) -> Optional[Tuple[float, float, float]]:
    """start에서 시즌 종료까지 하루 추세를 이어 그린 (낮은 값, 가운데, 높은 값).
    기울기 범위는 최근 14일 두 점 기울기의 가운데 절반과 최근 7일, 전체 추세를 합친 것.
    하루 값 흔들림만큼 더 넓힘"""
    if len(points) < min_days or end <= now:
        return None
    pairs = _pair_slopes(points[-14:])
    slopes = [pairs[len(pairs) // 4], pairs[len(pairs) * 3 // 4], _theil_sen(points[-7:]), _theil_sen(points)]
    mid = statistics.median(pairs)
    noise = statistics.median(abs(points[i][1] - points[i - 1][1] - mid) for i in range(1, len(points)))
    days = (end - now).total_seconds() / 86400
    return (start + min(slopes) * days - noise, start + mid * days, start + max(slopes) * days + noise)
