"""
서버 알림. 패치 노트, 점검, 매일 이터컷을 서버가 지정한 채널에 올림
"""
import asyncio
import logging
import re
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
WINDOW = re.compile(r'점검\s*시간\s*:\s*(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})\s*~\s*'
                    r'(?:(\d{4})-(\d{1,2})-(\d{1,2})\s+)?(\d{1,2}):(\d{2})')
WEEKDAYS = '월화수목금토일'
# 공지가 늦게 수정돼도 오래된 글은 알리지 않음
RECENT = 3 * 86400


def hour_label(hour: int) -> str:
    return f"{'오전' if hour < 12 else '오후'} {hour % 12 or (12 if hour else 0)}시"


def time_label(t: datetime) -> str:
    return hour_label(t.hour) + (f" {t.minute}분" if t.minute else "")


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


def maintenance_kind(title: str) -> Optional[str]:
    if '점검' not in title or '확률' in title:
        return None
    if '완료' in title:
        return 'done'
    if '연장' in title:
        return 'extended'
    return 'notice'


async def maintenance_window(article_id: int) -> Optional[str]:
    """공지 본문의 점검 시간. 형식이 다르면 None"""
    try:
        session = await api_client.get_session()
        async with session.get(ARTICLE.format(article_id)) as response:
            response.raise_for_status()
            page = await response.text()
    except Exception as e:
        logger.warning(f"점검 공지 본문 조회 실패: {article_id} {type(e).__name__}: {e}")
        return None
    m = WINDOW.search(re.sub(r'<[^>]+>', ' ', page))
    if not m:
        return None
    start = datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]))
    end_day = (int(m[6]), int(m[7]), int(m[8])) if m[6] else (start.year, start.month, start.day)
    end = datetime(*end_day, int(m[9]), int(m[10]))
    text = f"{start.month}월 {start.day}일 {WEEKDAYS[start.weekday()]}요일 {time_label(start)}부터 "
    if end.date() != start.date():
        text += f"{end.month}월 {end.day}일 "
    return text + f"{time_label(end)}까지"


def patch_layout(article: dict) -> ui.LayoutView:
    view = ui.LayoutView(timeout=None)
    children = [ui.TextDisplay(f"### {_title(article)}")]
    if article.get('thumbnail_url'):
        children.append(ui.MediaGallery(discord.MediaGalleryItem(article['thumbnail_url'])))
    view.add_item(ui.Container(*children, accent_colour=visual.colour('season')))
    view.add_item(ui.ActionRow(ui.Button(style=discord.ButtonStyle.link, label="핫픽스" if "핫픽스" in _title(article) else "패치 노트", emoji=EMOJIS['patch_note'],
                                         url=article.get('url') or ARTICLE.format(article['id']))))
    return view


def maintenance_layout(kind: str, article: dict, window: Optional[str]) -> ui.LayoutView:
    head = {'notice': '점검 안내', 'extended': '점검 연장', 'done': '점검 완료'}[kind]
    body = "게임을 다시 시작해주세요." if kind == 'done' else window or _title(article)
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(ui.TextDisplay(f"### {head}\n{body}"),
                               accent_colour=visual.colour('concurrent' if kind == 'done' else 'season')))
    view.add_item(ui.ActionRow(ui.Button(style=discord.ButtonStyle.link, label="공지", emoji=EMOJIS['web'],
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
        for name, check in (('패치 노트 확인', self.check_patches), ('점검 공지 확인', self.check_maintenance)):
            try:
                await check()
                self._failures[name] = 0
            except Exception as e:
                self.note_failure(name, e)

    async def check_patches(self):
        articles = (await api_client.get(NEWS.format('patchnote'), use_cache=False))['articles']
        ids = [a['id'] for a in articles]
        seen = guild_settings.state('seen_patches')
        # 처음 켤 때 이미 게시된 글은 확인한 것으로 기록
        if seen is None:
            guild_settings.set_state('seen_patches', ids)
            return
        new = [a for a in articles if a['id'] not in seen]
        if not new:
            return
        if not guild_settings.set_state('seen_patches', (ids + [i for i in seen if i not in ids])[:60]):
            return
        for article in sorted(new, key=lambda a: a['id']):
            if _recent(article):
                logger.info(f"패치 노트 알림 시작: {_title(article)}")
                await self.deliver('alert_patch', lambda: patch_layout(article))

    async def check_maintenance(self):
        articles = (await api_client.get(NEWS.format('news'), use_cache=False))['articles']
        found = {str(a['id']): (a, kind) for a in articles if (kind := maintenance_kind(_title(a)))}
        seen = guild_settings.state('seen_maintenance')
        if seen is None:
            guild_settings.set_state('seen_maintenance', {i: kind for i, (_, kind) in found.items()})
            return
        changed = [(a, kind) for i, (a, kind) in found.items() if seen.get(i) != kind]
        if not changed:
            return
        seen.update({str(a['id']): kind for a, kind in changed})
        if not guild_settings.set_state('seen_maintenance', dict(list(seen.items())[-30:])):
            return
        for article, kind in sorted(changed, key=lambda c: c[0]['id']):
            if not _recent(article):
                continue
            # 공지 제목 수정과 완료 공지가 겹치면 완료는 한 번만
            if kind == 'done':
                if time.time() - guild_settings.state('maintenance_done_at', 0) < 6 * 3600:
                    continue
                if not guild_settings.set_state('maintenance_done_at', time.time()):
                    continue
            window = None if kind == 'done' else await maintenance_window(article['id'])
            logger.info(f"점검 알림 시작: {kind} {_title(article)}")
            await self.deliver('alert_maintenance', lambda: maintenance_layout(kind, article, window))

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
