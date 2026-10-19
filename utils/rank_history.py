"""
아시아1 상위 순위와 이터컷의 시간별 기록. /랭킹 순위 변동과 /이터컷 추이용.
players는 상위 1000명의 마지막 RP 변화 시각으로, 휴면 계정 판별용
"""
import json
import os
import time
from typing import Dict, List, Optional, Tuple

from utils.logging_config import get_logger

logger = get_logger('순위기록')

PATH = 'data/rank_history.json'
INTERVAL = 3600
KEEP_RANKS = 49 * 3600
KEEP_CUTS = 120 * 86400
KEEP_PLAYERS = 14 * 86400

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


def record_cuts(season_id: int, eternity: Optional[int], demigod: Optional[int]) -> None:
    if not eternity and not demigod:
        return
    state = _load()
    now = time.time()
    cuts = [c for c in state['cuts'] if c[1] == season_id and now - c[0] < KEEP_CUTS]
    if cuts and now - cuts[-1][0] < INTERVAL:
        return
    cuts.append([now, season_id, eternity, demigod])
    state['cuts'] = cuts
    _save()


def cut_history(season_id: int) -> List[Tuple[float, Optional[int], Optional[int]]]:
    return [(c[0], c[2], c[3]) for c in _load()['cuts'] if c[1] == season_id]


def record_players(season_id: int, top_ranks: List[dict]) -> None:
    """닉네임별 [RP, 마지막 변화, 처음 본 시각, 마지막으로 본 시각]. 닉네임을 바꾸면 새 계정으로 보임"""
    state = _load()
    now = time.time()
    book = state['players']
    if book.get('season') != season_id:
        book = state['players'] = {'season': season_id, 'at': 0, 'list': {}}
    if now - book['at'] < INTERVAL:
        return
    players = book['list']
    for r in top_ranks:
        name, mmr = r.get('nickname'), r.get('mmr')
        if not name or mmr is None:
            continue
        old = players.get(name)
        if old is None:
            players[name] = [int(mmr), now, now, now]
        else:
            if old[0] != int(mmr):
                old[0], old[1] = int(mmr), now
            old[3] = now
    for name in [n for n, p in players.items() if now - p[3] > KEEP_PLAYERS]:
        del players[name]
    book['at'] = now
    _save()
