"""
아시아1 상위 순위와 이터컷의 시간별 기록. /랭킹 순위 변동과 /이터컷 추이용.
players는 상위 1000명의 마지막 RP 변화와 마지막 랭크 게임 시각으로, 휴면 계정 판별용
"""
import json
import math
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
GRACE = 12
# 정기 점검은 2026-08-13부터 격주 목요일
PATCH_ANCHOR = datetime(2026, 8, 13).date()
# 휴면 강등은 메테오라이트 4 아래로 내려가지 않음
DECAY_FLOOR = 6400
# 시즌 12 09-01~10-19 오전 9시 컷 백테스트로 고른 감쇠 추세 계수
ALPHA, BETA, DAMP = 0.5, 0.2, 0.95
# 막판 급등. 시즌 11 마지막 8일에 직전 7일 추세보다 300위 204, 1000위 162 더 올랐고 하루 상승은 2.9배, 2.7배.
# 앞 값은 그 초과분에 시즌 12 하루 게임 수 비율 0.87을 곱한 것
SURGE = {300: (177, 2.9), 1000: (141, 2.7)}
# 종료 7일 전부터 종료일까지 날마다 급등분의 누적 비율. 시즌 11 두 컷 평균
SURGE_SHAPE = (0.06, 0.18, 0.20, 0.24, 0.34, 0.41, 0.56, 1.0)
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
    # 매시 정각 뒤 첫 조회에 기록. 4분마다 조회해서 매시 0~4분
    if cuts and now // INTERVAL == cuts[-1][0] // INTERVAL:
        return
    cuts.append([now, season_id, eternity, demigod, active_eternity, active_demigod])
    state['cuts'] = cuts
    _save()


def cut_history(season_id: int) -> List[tuple]:
    """(시각, 이터니티, 데미갓, 휴면 강등 이터니티, 휴면 강등 데미갓). 시즌 12는 10-04 16시부터 기록.
    그 전 09-01~10-04 오전 9시 순위표 값과 10-05~10-19 오전 9시 휴면 강등 값은 게임 기록으로 복원"""
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
        # 10-19까지는 8일 전까지만 봐서 NO_RECENT가 실제 마지막 게임을 모르는 값
        if book.get('v') != 2 and p[LAST_GAME] == NO_RECENT:
            p[LAST_GAME] = p[CHECKED] = None
    book['v'] = 2
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


def decay_ticks(last: float, until: float) -> int:
    """마지막 랭크 게임 뒤 until까지 휴면 강등으로 100점씩 깎인 횟수. 시즌 12 게임 기록 398건으로 맞춘 규칙:
    다음 오전 9시부터 하루가 비면 그다음 15시에 유예 1을 쓰고, 유예 12를 다 쓰면 100점. 격주 목요일 패치 날은 건너뜀"""
    day = datetime.fromtimestamp(last, KST).replace(hour=9, minute=0, second=0, microsecond=0)
    if day.timestamp() <= last:
        day += timedelta(days=1)
    empty = 0
    while True:
        day += timedelta(days=1)
        if day.replace(hour=15).timestamp() >= until:
            break
        if not (day.weekday() == 3 and (day.date() - PATCH_ANCHOR).days % 14 == 0):
            empty += 1
    return max(0, empty - GRACE)


def projected_rp(p: list, mmr: int, now: float, end: float) -> Optional[int]:
    """7일 넘게 쉰 계정이 end까지 돌아오지 않으면 남을 RP. 이미 깎인 만큼은 mmr에 들어 있음. 활동 중이면 mmr"""
    at = last_active(p)
    if at is None:
        return None
    if now - at <= DORMANT_DAYS * 86400:
        return mmr
    # 게임 기록 끝까지 랭크 게임이 없으면 유예를 다 쓴 것으로 봄
    at = at if at > 0 else now - 60 * 86400
    return max(mmr - 100 * (decay_ticks(at, end) - decay_ticks(at, now)), min(mmr, DECAY_FLOOR))


def active_cut(season_id: int, top_ranks: List[dict], place: int, end: Optional[float] = None) -> Optional[Tuple[int, int, bool]]:
    """7일 넘게 쉰 계정을 end(시즌 종료)까지 휴면 강등된 RP로 다시 줄 세운 place번째 RP, 그 위에서 내려간 휴면 계정 수, 추정 여부.
    판별 안 된 계정이 20명 넘으면 None. 그 이하는 조회 실패나 새로 들어온 계정이라 활동으로 봄.
    목록이 1000위까지라 1000위 밖에서 올라올 계정은 알 수 없음. 활동 계정이 place명이 안 되면 끝부분 순위당 RP 감소폭과 활동 비율로 나머지를 추정"""
    players = player_book(season_id)
    book = _load()['players']
    since = book.get('since') or min((p[FIRST] for p in players.values()), default=0)
    now = time.time()
    end = end or now
    ranked = sorted((r for r in top_ranks if r.get('mmr')), key=lambda r: r.get('rank', 0))
    if len(ranked) < 200:
        return None
    unknown = 0
    scores: List[Tuple[int, int]] = []
    for r in ranked:
        mmr = int(r['mmr'])
        p = players.get(r.get('nickname'))
        value = projected_rp(p, mmr, now, end) if p else None
        if value is None:
            # 기록을 시작한 뒤 새로 들어온 계정은 올라왔거나 닉네임을 바꾼 것이라 활동으로 봄
            if p and p[FIRST] - since < 3600:
                unknown += 1
                if unknown > 20:
                    return None
            value = mmr
        scores.append((value, mmr))
    last = int(ranked[-1]['mmr'])
    # 1000위보다 낮아진 휴면 계정은 목록 밖 계정과 순서를 알 수 없어 줄에서 뺌
    kept = sorted((v for v, _ in scores if v >= last), reverse=True)
    dropped = lambda cut: sum(1 for v, mmr in scores if v < cut <= mmr)
    if len(kept) >= place:
        value = kept[place - 1]
        return value, dropped(value), False
    window = ranked[-100:]
    per_rank = (int(window[0]['mmr']) - last) / (len(window) - 1)
    active_share = sum(v >= last for v, _ in scores[-100:]) / 100
    if active_share <= 0:
        return None
    value = round(last - per_rank * (place - len(kept)) / active_share)
    return value, dropped(value), True


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


def _theil_sen(values: List[float]) -> float:
    return statistics.median((values[j] - values[i]) / (j - i) for i in range(len(values)) for j in range(i + 1, len(values)))


def forecast(points: List[Tuple[datetime, float]], start: float, now: datetime, end: datetime,
             surge: Tuple[float, float] = (0, 1), min_days: int = 7) -> Optional[dict]:
    """오전 9시 값의 감쇠 추세(Holt, 날마다 기울기 0.95배)로 start에서 시즌 종료까지 날마다 오전 9시 값을 예상.
    가운데 값의 막판 급등은 시즌 11 초과분을 그대로 더한 값과 시즌 12 추세에 배율을 곱한 값의 평균.
    범위는 백테스트 80% 오차(24일 뒤 약 67)라 멀수록 넓어지고, 낮은 값은 급등 없이, 높은 값은 초과분 그대로.
    반환은 날마다 (시각, 가운데, 낮은 값, 높은 값)"""
    if len(points) < min_days or end <= now:
        return None
    values = [v for _, v in points]
    level, trend = values[0], _theil_sen(values[:7])
    for v in values[1:]:
        last = level
        level = ALPHA * v + (1 - ALPHA) * (level + DAMP * trend)
        trend = BETA * (level - last) + (1 - BETA) * DAMP * trend
    days = []
    day = now.astimezone(KST).replace(hour=9, minute=0, second=0, microsecond=0)
    if day <= now:
        day += timedelta(days=1)
    while day <= end:
        h = (day - now).total_seconds() / 86400
        days.append((day, h, trend * sum(DAMP ** k for k in range(1, math.ceil(h) + 1)) * h / math.ceil(h)))
        day += timedelta(days=1)
    if not days:
        return None
    extra, ratio = surge
    late = days[-1][2] - (days[-9][2] if len(days) > 8 else 0)
    middle = (extra + (ratio - 1) * max(late, 0)) / 2
    future = []
    for day, h, gain in days:
        left = round((end - day).total_seconds() / 86400)
        share = SURGE_SHAPE[len(SURGE_SHAPE) - 1 - left] if left < len(SURGE_SHAPE) else 0
        half = 6.2 * h ** 0.75
        # 컷은 시즌 내내 내려간 적이 거의 없어 낮은 값은 지금 값까지만
        future.append((day, start + gain + share * middle, max(start + gain - half, start), start + gain + half + share * extra))
    return {'future': future}
