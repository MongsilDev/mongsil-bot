"""
서버 알림. 공식 소식 6개 분류와 매일 이터컷을 서버가 지정한 채널에 올림
"""
import asyncio
import logging
import time
from datetime import datetime
from typing import Callable, Optional, Set
from zoneinfo import ZoneInfo

import discord
from discord import ui
from discord.ext import commands, tasks

from client import ERClient
from utils import guild_settings, visual
from utils.api_client import api_client
from utils.emojis import EMOJIS
from utils.logging_config import get_logger

logger = get_logger('알림')

KST = ZoneInfo('Asia/Seoul')
NEWS = 'https://playeternalreturn.com/api/v1/posts/news?category={}&page=1&hl=ko-KR'
ARTICLE = 'https://playeternalreturn.com/posts/news/{}?hl=ko-KR'
# 공지가 늦게 수정돼도 오래된 글은 알리지 않음
RECENT = 3 * 86400


def hour_label(hour: int) -> str:
    return f"{'오전' if hour < 12 else '오후'} {hour % 12 or (12 if hour else 0)}시"


def channel_ok(channel, attach: bool) -> list:
    """알림 채널에서 몽실봇에 없는 권한 이름"""
    perms = channel.permissions_for(channel.guild.me)
    need = [('채널 보기', perms.view_channel), ('메시지 보내기', perms.send_messages)]
    if attach:
        need.append(('파일 첨부', perms.attach_files))
    return [name for name, ok in need if not ok]


def _title(article: dict) -> str:
    i18n = article['i18ns'].get('ko_KR') or next(iter(article['i18ns'].values()))
    return i18n['title'].strip()


def _recent(article: dict) -> bool:
    try:
        created = datetime.fromisoformat(article['created_at'].replace('Z', '+00:00'))
    except (KeyError, ValueError):
        return True
    return time.time() - created.timestamp() < RECENT


def news_layout(article: dict, name: str, emoji: str) -> ui.LayoutView:
    view = ui.LayoutView(timeout=None)
    children = [ui.TextDisplay(f"### {_title(article)}")]
    if article.get('thumbnail_url'):
        children.append(ui.MediaGallery(discord.MediaGalleryItem(article['thumbnail_url'])))
    view.add_item(ui.Container(*children, accent_colour=visual.colour('season')))
    view.add_item(ui.ActionRow(ui.Button(style=discord.ButtonStyle.link, label=name, emoji=emoji,
                                         url=article.get('url') or ARTICLE.format(article['id']))))
    return view


class Alerts(commands.Cog):
    def __init__(self, client: ERClient):
        self.client = client
        self._cut_view = None
        self._failures: dict = {}
        self._cut_key = None
        self.watch_news.start()
        self.daily_cut.start()

    def cog_unload(self):
        self.watch_news.cancel()
        self.daily_cut.cancel()

    def turn_off(self, guild_id: int, key: str, reason: str):
        guild_settings.put(guild_id, key, 0)
        guild_settings.log(guild_id, f"{guild_settings.ALERT_NAMES[key]} 알림: 끔, {reason}", source='bot')
        logger.warning(f"알림 끔: {guild_id} {key} {reason}")

    async def deliver(self, key: str, make: Callable[[], ui.LayoutView], only: Optional[Set[int]] = None) -> int:
        targets = [(g, c) for g, c in guild_settings.guilds_with(key) if only is None or g in only]
        sent = failed = skipped = 0
        for guild_id, channel_id in targets:
            guild = self.client.get_guild(guild_id)
            # 기동 직후나 디스코드 장애 때는 채널 정보가 없어 삭제로 오판하지 않게 건너뜀
            if guild is None or guild.unavailable:
                skipped += 1
                continue
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                self.turn_off(guild_id, key, "채널 삭제됨")
                failed += 1
                continue
            view = make()
            try:
                await channel.send(view=view, files=visual.files_of(view))
                sent += 1
            except discord.Forbidden:
                self.turn_off(guild_id, key, "채널 권한 없음")
                failed += 1
            except Exception as e:
                logger.warning(f"알림 전송 실패: {guild_id} {key} {type(e).__name__}: {e}")
                failed += 1
            await asyncio.sleep(0.2)
        if targets:
            logger.info(f"알림 전송 {key}: 보냄 {sent}, 실패 {failed}, 건너뜀 {skipped}")
        return sent

    def note_failure(self, name: str, e: Exception):
        """연속 6번 실패하면 ERROR로 올려 Sentry에 남김. 공식 사이트 형식이 바뀐 경우 등"""
        count = self._failures.get(name, 0) + 1
        self._failures[name] = count
        level = logging.ERROR if count == 6 else logging.WARNING
        logger.log(level, f"{name} 실패 {count}회 연속: {type(e).__name__}: {e}")

    @tasks.loop(minutes=5)
    async def watch_news(self):
        for key, path, name in guild_settings.NEWS:
            try:
                await self.check_category(key, path, name)
                self._failures[name] = 0
            except Exception as e:
                self.note_failure(name, e)

    async def check_category(self, key: str, path: str, name: str):
        articles = (await api_client.get(NEWS.format(path), use_cache=False))['articles']
        ids = [a['id'] for a in articles]
        # 패치노트는 분류를 나누기 전 기록을 이어 써서 배포 직후 지난 글을 다시 보내지 않음
        state_key = 'seen_patches' if key == 'alert_patch' else f'seen_{path}'
        seen = guild_settings.state(state_key)
        # 처음 켤 때 이미 게시된 글은 확인한 것으로 기록
        if seen is None:
            guild_settings.set_state(state_key, ids)
            return
        new = [a for a in articles if a['id'] not in seen]
        if not new:
            return
        if not guild_settings.set_state(state_key, (ids + [i for i in seen if i not in ids])[:60]):
            return
        emoji = EMOJIS['patch_note'] if key == 'alert_patch' else EMOJIS['web']
        for article in sorted(new, key=lambda a: a['id']):
            # 제목이 빈 글은 공식 사이트 첫 화면으로 넘어감
            if _recent(article) and _title(article):
                logger.info(f"{name} 알림 시작: {_title(article)}")
                await self.deliver(key, lambda: news_layout(article, name, emoji))

    async def cut_view(self) -> Optional[ui.LayoutView]:
        from commands.rating import create_rating_layout, fetch_rating_info, season_end_for
        from commands.season import get_ranked_season, get_season_info
        from utils.rank_helpers import fetch_ranking_data
        season = await get_ranked_season()
        if not season:
            return None
        season_id, season_name = season
        (rank_300, rank_1000), info, top = await asyncio.gather(
            fetch_rating_info(self.client, season_id), get_season_info(), fetch_ranking_data(self.client, season_id))
        if not rank_300 and not rank_1000:
            return None
        return create_rating_layout(rank_300, rank_1000, season_name, season_id, season_end_for(info, season_id), top=top)

    @tasks.loop(minutes=1)
    async def daily_cut(self):
        try:
            await self.post_daily_cut()
        except Exception as e:
            self.note_failure('매일 이터컷', e)

    async def post_daily_cut(self):
        subs = guild_settings.guilds_with('alert_cut')
        if not subs:
            return
        now = datetime.now(KST)
        today = now.date().isoformat()
        posted = guild_settings.state('cut_posted', {})
        due = {gid for gid, _ in subs if guild_settings.get(gid, 'cut_hour') == now.hour and posted.get(str(gid)) != today}
        if not due:
            return
        # 같은 시각 서버들은 같은 카드를 받음
        if self._cut_key != (today, now.hour) or self._cut_view is None:
            first = self._cut_key != (today, now.hour)
            self._cut_view, self._cut_key = await self.cut_view(), (today, now.hour)
            if self._cut_view is None:
                if first:
                    logger.info(f"매일 이터컷 {now.hour}시: 컷 정보가 없어 다음 분에 다시 시도")
                return
        posted.update({str(gid): today for gid in due})
        if not guild_settings.set_state('cut_posted', posted):
            return
        view = self._cut_view
        await self.deliver('alert_cut', lambda: view, only=due)
        self._failures['매일 이터컷'] = 0

    @watch_news.before_loop
    @daily_cut.before_loop
    async def _before(self):
        await self.client.wait_until_ready()


async def setup(client: ERClient):
    await client.add_cog(Alerts(client))
