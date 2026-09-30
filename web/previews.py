"""
첫 화면의 명령어 출력 미리보기. 봇과 같은 레이아웃 함수로 실데이터를 그려 30분마다 갱신
"""
import asyncio
import html
import time

from utils.logging_config import get_logger
from web.discord_render import render_view

logger = get_logger('미리보기')

REFRESH_SECONDS = 1800

# 명령어 이름별 입력 표시와 렌더 결과
previews: dict[str, dict] = {}
# 첫 화면 숫자. 동접, 컷, 시즌 종료일
live: dict = {}
updated_at = 0.0


async def _build(client) -> None:
    from commands.concurrent import concurrent_data, create_concurrent_layout, get_current_player_count
    from commands.info import create_bot_info_layout
    from commands.playtime import create_playtime_layout, get_playtime_info
    from commands.rank import build_rank_view
    from commands.ranking import PaginationView, get_ranking_info
    from commands.rating import create_rating_layout, cut_rp, fetch_rating_info
    from commands.season import create_season_layout, fetch_patch_notes, get_ranked_season, get_season_info
    from commands.settings import SettingsView
    from utils.animal_utils import fetch_animal_image
    from utils.rank_helpers import fetch_ranking_data

    class RankingPreview(PaginationView):
        # 미리보기는 1페이지만 쓰므로 다음 페이지 선조회를 막음
        def _prefetch(self, page):
            pass

    async def attempt(name, make):
        try:
            result = await make()
            if result:
                previews[name] = result
        except Exception as e:
            logger.warning(f"{name} 미리보기 실패: {type(e).__name__} {e}")

    season = await get_ranked_season()
    if not season:
        return
    season_id, season_name = season
    top = await fetch_ranking_data(client, season_id) or []
    leader = top[0]['nickname'] if top else None

    async def rank():
        return leader and {'input': leader, 'html': render_view(await build_rank_view(client, leader))}

    async def ranking():
        users = await get_ranking_info(client, season_id, 1)
        return users and {'input': '', 'html': render_view(RankingPreview(client, season_id, 10, users, season_name))}

    async def rating():
        a, b = await fetch_rating_info(client, season_id)
        live['eternity'], live['demigod'] = cut_rp(a), cut_rp(b)
        info = await get_season_info()
        view = create_rating_layout(a, b, season_name, season_id, info.end_date if info else None, top=top)
        return {'input': '', 'html': render_view(view)}

    async def playtime():
        stats = leader and await get_playtime_info(client, leader)
        return stats and {'input': leader, 'html': render_view(create_playtime_layout(stats))}

    async def season_card():
        info = await get_season_info()
        if info:
            live['season_name'], live['season_start'], live['season_end'] = info.name, info.start_date, info.end_date
        return info and {'input': '', 'html': render_view(create_season_layout(info, await fetch_patch_notes()))}

    async def concurrent():
        count = await get_current_player_count()
        if count is None:
            return None
        live['players'] = count
        # 24시간 추이 스파크라인용, 30분 간격으로 줄임
        samples = [round(c) for _, c in concurrent_data.series(hours=24, bucket_minutes=30)]
        live['players_series'] = samples + [count]
        return {'input': '', 'html': render_view(create_concurrent_layout(count))}

    def animal(url, name):
        async def make():
            image = await fetch_animal_image(url, name)
            if not image:
                return None
            src = html.escape(image['url'])
            return {'input': '', 'html': f'<img class="dc-image" src="{src}" alt="{name} 사진" loading="lazy" decoding="async">'}
        return make

    async def info():
        return {'input': '', 'html': render_view(create_bot_info_layout(client))}

    async def settings():
        # 실제 서버 설정을 읽지 않도록 존재하지 않는 서버 ID로 그림
        return {'input': '', 'html': render_view(SettingsView(0))}

    # bser API 부담을 나누려고 순서대로 실행
    for name, make in [
        ('랭크', rank), ('랭킹', ranking), ('이터컷', rating), ('플탐', playtime), ('시즌', season_card),
        ('동접', concurrent), ('정보', info), ('설정', settings),
        ('강아지', animal('https://api.thedogapi.com/v1/images/search', '강아지')),
        ('고양이', animal('https://api.thecatapi.com/v1/images/search', '고양이')),
    ]:
        await attempt(name, make)


async def refresh_loop(client) -> None:
    global updated_at
    await client.wait_until_ready()
    while True:
        started = time.monotonic()
        try:
            await _build(client)
            updated_at = time.time()
            logger.info(f"미리보기 갱신 {len(previews)}개, {time.monotonic() - started:.1f}초")
        except Exception:
            logger.error("미리보기 갱신 실패", exc_info=True)
        await asyncio.sleep(REFRESH_SECONDS)
