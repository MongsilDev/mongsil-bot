"""
아시아1 상위 순위와 이터컷의 시간별 기록. /랭킹 순위 변동과 /이터컷 추이용
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
KEEP_CUTS = 15 * 86400

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
    """24시간 전에 가장 가까운 20시간 이상 지난 기록"""
    now = time.time()
    old = [r for r in _load()['ranks'] if r[1] == season_id and now - r[0] >= 20 * 3600]
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
