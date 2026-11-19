"""
이벤트 루프가 STALL_SECONDS 넘게 멈추면 그때 루프 스레드가 실행 중이던 스택을 로그에 남김.
discord.py는 10초 넘게 막혀야 경고를 남겨서 3~8초짜리 멈춤으로 명령이 만료돼도 원인이 남지 않았음(장부 518)
"""
import asyncio
import sys
import threading
import time
import traceback
from typing import Optional

from .logging_config import get_logger

logger = get_logger('loop_watch')

BEAT_SECONDS = 0.25
CHECK_SECONDS = 0.5
STALL_SECONDS = 1.0

_beat = time.monotonic()
_thread: Optional[threading.Thread] = None
_task: Optional[asyncio.Task] = None


async def _heartbeat() -> None:
    global _beat
    while True:
        now = time.monotonic()
        late = now - _beat - BEAT_SECONDS
        if late > STALL_SECONDS:
            logger.warning(f"이벤트 루프 {late:.1f}초 지연")
        _beat = now
        await asyncio.sleep(BEAT_SECONDS)


def _watch(loop_thread_id: int) -> None:
    reported = 0.0
    last_check = time.monotonic()
    while True:
        time.sleep(CHECK_SECONDS)
        now = time.monotonic()
        # 감시 스레드도 함께 늦었으면 VM이나 프로세스 전체가 멈춘 것이라 루프 스택은 원인이 아님
        own_late = now - last_check - CHECK_SECONDS
        last_check = now
        stalled = now - _beat
        if stalled < STALL_SECONDS or reported == _beat:
            continue
        reported = _beat
        if own_late > STALL_SECONDS:
            logger.warning(f"프로세스 전체 {own_late:.1f}초 멈춤")
            continue
        frame = sys._current_frames().get(loop_thread_id)
        stack = ''.join(traceback.format_stack(frame, limit=12)) if frame else '(스택 없음)'
        logger.warning(f"이벤트 루프 {stalled:.1f}초째 막힘. 실행 중인 코드:\n{stack}")


def start() -> None:
    """실행 중인 이벤트 루프 안에서 한 번 호출"""
    global _thread, _task, _beat
    if _thread:
        return
    _beat = time.monotonic()
    _task = asyncio.get_running_loop().create_task(_heartbeat())
    _thread = threading.Thread(target=_watch, args=(threading.get_ident(),), name='loop-watch', daemon=True)
    _thread.start()
