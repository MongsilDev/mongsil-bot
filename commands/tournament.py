"""
Labs 점수 계산기 대회 순위 이미지. 순위 계산과 그림은 Labs가 함
"""
import asyncio
import json
import os
import re
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

import aiohttp
import discord
from discord import app_commands, ui
from discord.ext import commands, tasks

from client import ERClient
from utils import visual
from utils.api_client import api_client
from utils.config import config
from utils.emojis import EMOJIS
from utils.errors import NotFoundError, ServiceDownError, ValidationError, handle_errors
from utils.layouts import create_error_layout, send_card
from utils.logging_config import get_logger
from utils.usage_db import db

logger = get_logger('대회')

API = f"{config.labs_url}/api/bot"
CODE = re.compile(r'^(?:[2-9A-HJKMNP-Z]{6}|EXAMPLE)$')
LINK = re.compile(r'/er/(?:t/([0-9A-Za-z]{6})|(example))(?:/([A-Ha-h]))?(?:[/?#]|$)')
GROUPS = 'ABCDEFGH'
MODE = {'points': '포인트제', 'checkpoint': '체크포인트', 'double': '더블 체크포인트'}

LIVE_SECONDS = 30
LIVE_PER_GUILD = 3
LIVE_PER_USER = 3
LIVE_TOTAL = 50
REFRESH_GAP = 5.0
TOO_SOON = "잠시 후 다시 눌러주세요."

BAD_CODE = "대회 코드는 영문과 숫자 6자리입니다.\n-# Labs 대회 화면의 코드나 주소를 그대로 붙여 넣어도 됩니다."
GONE = "없거나 지워진 대회입니다.\n-# 대회는 마지막 수정 뒤 24시간이 지나면 지워집니다."
DOWN = "Labs에 연결하지 못했습니다.\n잠시 후 다시 시도해주세요."
DUPLICATE = "이 채널에서 이미 이 조의 순위를 올리고 있습니다."

UNCHANGED = object()

db.executescript("""
CREATE TABLE IF NOT EXISTS live_boards (
    message_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    guild_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    grp TEXT NOT NULL,
    version INTEGER NOT NULL,
    started_by INTEGER NOT NULL,
    started_at INTEGER NOT NULL,
    board TEXT NOT NULL,
    image_id INTEGER,
    last_round INTEGER NOT NULL DEFAULT 0
);
""")
_columns = {r[1] for r in db.execute("PRAGMA table_info(live_boards)")}
if 'image_id' not in _columns:
    db.execute("ALTER TABLE live_boards ADD COLUMN image_id INTEGER")
if 'last_round' not in _columns:
    db.execute("ALTER TABLE live_boards ADD COLUMN last_round INTEGER NOT NULL DEFAULT 0")


def parse_target(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """대회 코드와 조. Labs 주소를 붙여 넣으면 주소에서 꺼냄"""
    text = (text or '').strip()
    m = LINK.search(text)
    if m:
        code = 'EXAMPLE' if m[2] else m[1].upper()
        return (code, m[3].upper() if m[3] else None) if CODE.match(code) else None
    code = re.sub(r'\s+', '', text).upper()
    return (code, None) if CODE.match(code) else None


async def _labs_get(code: str, path: str, params: dict, group: Optional[str], image: bool = False):
    token = os.getenv('LABS_BOT_TOKEN')
    if not token:
        raise ServiceDownError("LABS_BOT_TOKEN 없음", DOWN)
    session = await api_client.get_session()
    try:
        async with session.get(f"{API}/t/{code}{path}", params=params, headers={'x-bot-token': token},
                               timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status == 204:
                return UNCHANGED
            if r.status == 404:
                raise NotFoundError(f"대회 없음 {code}", GONE)
            if r.status == 400:
                raise ValidationError(f"없는 조 {code} {group}", f"이 대회에는 {group}조가 없습니다.")
            if r.status != 200:
                raise ServiceDownError(f"Labs 응답 {r.status}", DOWN)
            return await (r.read() if image else r.json())
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        raise ServiceDownError(f"Labs 연결 실패: {type(e).__name__}", DOWN) from e


async def fetch_board(code: str, group: Optional[str] = None, user_id: Optional[int] = None,
                      since: Optional[int] = None):
    """Labs가 계산한 조 순위. 바뀐 게 없으면 UNCHANGED"""
    params = {k: str(v) for k, v in (('group', group), ('user', user_id), ('since', since)) if v}
    return await _labs_get(code, '', params, group)


async def fetch_image(code: str, group: str) -> bytes:
    """Labs가 그린 조 순위 PNG"""
    return await _labs_get(code, '/image', {'group': group}, group, image=True)


_mine: Dict[int, Tuple[float, list]] = {}


async def my_tournaments(user_id: int) -> list:
    """자동 완성용. 사용자가 개최자나 운영자인 대회. 실패하면 빈 목록"""
    hit = _mine.get(user_id)
    if hit and time.monotonic() - hit[0] < 30:
        return hit[1]
    token = os.getenv('LABS_BOT_TOKEN')
    if not token:
        return []
    try:
        session = await api_client.get_session()
        async with session.get(f"{API}/users/{user_id}/tournaments", headers={'x-bot-token': token},
                               timeout=aiohttp.ClientTimeout(total=2)) as r:
            if r.status != 200:
                return []
            rows = await r.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return []
    _mine[user_id] = (time.monotonic(), rows)
    if len(_mine) > 1000:
        _mine.clear()
    return rows


# 메시지

def next_lines(n: dict) -> List[str]:
    weather = ', '.join(w for w in (n.get('main'), n.get('sub')) if w)
    lines = [f"-# 다음 {n['roundNo']}라운드" + (f" | 날씨 {weather}" if weather else '')]
    if n.get('bans'):
        lines.append('-# 금지 실험체 ' + ', '.join(n['bans']))
    return lines


def link_button(b: dict) -> ui.Button:
    return ui.Button(style=discord.ButtonStyle.link, label="순위표", emoji=EMOJIS['labs'], url=b['url'])


def gallery(view: ui.LayoutView, png: bytes) -> ui.MediaGallery:
    return ui.MediaGallery(discord.MediaGalleryItem(visual.attach(view, 'tournament.png', png)))


def notice_view(b: dict, state: str = 'on') -> ui.LayoutView:
    """자동 갱신 안내. state: 'on', 'off', 'ended', 'gone', 'nogroup'"""
    title = b['name'] + (f" {b['group']}조" if b['groupCount'] > 1 else '')
    if state == 'on':
        rounds = (f"{b['played']}/{b['plannedRounds']}라운드" if b['mode'] == 'points' and not b['ended']
                  else f"{b['played']}라운드")
        line = f"{MODE.get(b['mode'], b['mode'])} | {rounds} | 자동 갱신 중"
    else:
        line = {'off': "자동 갱신을 껐습니다.", 'ended': "대회가 끝나 자동 갱신을 멈췄습니다.",
                'gone': "대회가 지워져 자동 갱신을 멈췄습니다.",
                'nogroup': f"{b['group']}조가 없어져 자동 갱신을 멈췄습니다."}[state]
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(ui.TextDisplay(f"### {discord.utils.escape_markdown(title)}\n-# {line}"),
                               accent_colour=visual.colour('tournament')))
    row = [LiveOffButton(b['code'], b['group'])] if state == 'on' else []
    view.add_item(ui.ActionRow(*row, link_button(b)))
    return view


def round_view(b: dict, png: bytes) -> ui.LayoutView:
    """자동 갱신이 채널에 올리는 라운드 순위 이미지"""
    view = ui.LayoutView(timeout=None)
    children = [gallery(view, png)]
    if b.get('next') and not b['ended']:
        children.append(ui.TextDisplay('\n'.join(next_lines(b['next']))))
    view.add_item(ui.Container(*children, accent_colour=visual.colour('tournament')))
    view.add_item(ui.ActionRow(link_button(b)))
    return view


def card_view(b: dict, png: bytes) -> ui.LayoutView:
    """자동 갱신 없이 한 번 보는 카드"""
    view = ui.LayoutView(timeout=None)
    children = [gallery(view, png)]
    foot = next_lines(b['next']) if b.get('next') and not b['ended'] else []
    if b.get('updatedAt'):
        foot.append(f"-# 마지막 수정 <t:{b['updatedAt'] // 1000}:R>")
    if foot:
        children.append(ui.TextDisplay('\n'.join(foot)))
    view.add_item(ui.Container(*children, accent_colour=visual.colour('tournament')))
    if b['groupCount'] > 1:
        view.add_item(ui.ActionRow(GroupSelect(b['code'], b['groups'], b['group'])))
    view.add_item(ui.ActionRow(RefreshButton(b['code'], b['group']), link_button(b)))
    return view


# 자동 갱신 기록. message_id는 안내 메시지, image_id는 마지막으로 올린 순위 이미지 메시지

def live_get(message_id: int) -> Optional[sqlite3.Row]:
    return db.execute("SELECT * FROM live_boards WHERE message_id = ?", (message_id,)).fetchone()


def live_put(message: discord.Message, guild_id: int, b: dict, user_id: int, image_id: Optional[int],
             last_round: int) -> None:
    db.execute("INSERT OR REPLACE INTO live_boards (message_id, channel_id, guild_id, code, grp, version, "
               "started_by, started_at, board, image_id, last_round) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (message.id, message.channel.id, guild_id, b['code'], b['group'], b['version'], user_id,
                int(time.time()), json.dumps(b, ensure_ascii=False), image_id, last_round))


def live_save(message_id: int, b: dict, image_id: Optional[int], last_round: int) -> None:
    db.execute("UPDATE live_boards SET version = ?, board = ?, image_id = ?, last_round = ? WHERE message_id = ?",
               (b['version'], json.dumps(b, ensure_ascii=False), image_id, last_round, message_id))


def live_drop(message_id: int) -> None:
    db.execute("DELETE FROM live_boards WHERE message_id = ?", (message_id,))


def live_running(channel_id: int, code: str, group: str) -> bool:
    return db.execute("SELECT 1 FROM live_boards WHERE channel_id = ? AND code = ? AND grp = ?",
                      (channel_id, code, group)).fetchone() is not None


def is_operator(b: dict) -> bool:
    return b.get('role') in ('owner', 'operator')


async def reply_error(interaction: discord.Interaction, text: str) -> None:
    view = create_error_layout(text)
    if interaction.response.is_done():
        await interaction.followup.send(view=view, ephemeral=True)
    else:
        await interaction.response.send_message(view=view, ephemeral=True)


_last_refresh: Dict[object, float] = {}


def too_soon(key) -> bool:
    """새로고침과 조 선택은 메시지마다와 사람마다, 자동 갱신 끄기는 사람마다 REFRESH_GAP 간격"""
    now = time.monotonic()
    if now - _last_refresh.get(key, 0) < REFRESH_GAP:
        return True
    _last_refresh[key] = now
    if len(_last_refresh) > 5000:
        _last_refresh.clear()
    return False


async def card_too_soon(interaction: discord.Interaction) -> bool:
    # 같은 카드의 연속 클릭은 조용히 넘기고, 카드를 번갈아 누르는 사람에게만 안내. 통과한 클릭만 두 간격을 기록
    if time.monotonic() - _last_refresh.get(interaction.message.id, 0) < REFRESH_GAP:
        await interaction.response.defer()
        return True
    if too_soon(('card', interaction.user.id)):
        await reply_error(interaction, TOO_SOON)
        return True
    too_soon(interaction.message.id)
    return False


def live_limit(guild_id: int, user_id: int) -> Optional[str]:
    rows = db.execute("SELECT guild_id, started_by FROM live_boards").fetchall()
    if sum(r['guild_id'] == guild_id for r in rows) >= LIVE_PER_GUILD:
        return (f"이 서버에서는 자동 갱신을 {LIVE_PER_GUILD}개까지 켤 수 있습니다.\n"
                "다른 카드의 자동 갱신을 끈 뒤 다시 시도해주세요.")
    if sum(r['started_by'] == user_id for r in rows) >= LIVE_PER_USER:
        return (f"자동 갱신은 한 사람이 {LIVE_PER_USER}개까지 켤 수 있습니다.\n"
                "다른 카드의 자동 갱신을 끈 뒤 다시 시도해주세요.")
    if len(rows) >= LIVE_TOTAL:
        return "지금은 자동 갱신을 더 켤 수 없습니다.\n잠시 후 다시 시도해주세요."
    return None


def live_blocked(interaction: discord.Interaction) -> Optional[str]:
    """자동 갱신을 켤 수 없는 이유. 봇 채널 권한과 개수 제한"""
    perms = interaction.app_permissions
    send = perms.send_messages_in_threads if isinstance(interaction.channel, discord.Thread) else perms.send_messages
    missing = [name for name, ok in (('채널 보기', perms.view_channel), ('메시지 보내기', send),
                                     ('파일 첨부', perms.attach_files)) if not ok]
    if missing:
        names = ', '.join(missing)
        return (f"몽실봇에 이 채널의 {names} 권한이 없어 자동 갱신을 켤 수 없습니다.\n"
                f"채널 권한에서 몽실봇에 {names}를 허용해주세요.")
    return live_limit(interaction.guild.id, interaction.user.id)


async def redraw(interaction: discord.Interaction, code: str, group: str) -> None:
    """한 번 보기 카드를 새 이미지로 다시 그림"""
    await interaction.response.defer()
    try:
        b = await fetch_board(code, group)
        view = card_view(b, await fetch_image(code, b['group']))
    except (NotFoundError, ServiceDownError, ValidationError) as e:
        await reply_error(interaction, e.user_message)
        return
    await interaction.edit_original_response(view=view, attachments=visual.files_of(view))


class RefreshButton(ui.DynamicItem[ui.Button], template=r'tr:(?P<code>[0-9A-Z]{6,7}):(?P<g>[A-H])'):
    def __init__(self, code: str, group: str):
        super().__init__(ui.Button(label="새로고침", emoji="🔄", style=discord.ButtonStyle.secondary,
                                   custom_id=f"tr:{code}:{group}"))
        self.code, self.group = code, group

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match['code'], match['g'])

    async def callback(self, interaction: discord.Interaction):
        if await card_too_soon(interaction):
            return
        await redraw(interaction, self.code, self.group)


class GroupSelect(ui.DynamicItem[ui.Select], template=r'tg:(?P<code>[0-9A-Z]{6,7}):(?P<n>[1-8])'):
    def __init__(self, code: str, groups: List[str], current: str = 'A'):
        options = [discord.SelectOption(label=f"{g}조", value=g, default=g == current) for g in groups]
        super().__init__(ui.Select(placeholder="조 선택", options=options, custom_id=f"tg:{code}:{len(groups)}"))
        self.code = code

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match['code'], list(GROUPS[:int(match['n'])]))

    async def callback(self, interaction: discord.Interaction):
        if await card_too_soon(interaction):
            return
        await redraw(interaction, self.code, self.item.values[0])


class LiveButton(ui.DynamicItem[ui.Button], template=r'ta:(?P<code>[0-9A-Z]{6,7}):(?P<g>[A-H])'):
    """배포 전 카드에 남은 자동 갱신 버튼"""
    def __init__(self, code: str, group: str):
        super().__init__(ui.Button(label="자동 갱신", style=discord.ButtonStyle.secondary,
                                   custom_id=f"ta:{code}:{group}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match['code'], match['g'])

    async def callback(self, interaction: discord.Interaction):
        await reply_error(interaction, "/대회를 다시 입력하면 자동 갱신이 켜집니다.")


class LiveOffButton(ui.DynamicItem[ui.Button], template=r'tx:(?P<code>[0-9A-Z]{6,7}):(?P<g>[A-H])'):
    def __init__(self, code: str, group: str):
        super().__init__(ui.Button(label="자동 갱신 끄기", style=discord.ButtonStyle.secondary,
                                   custom_id=f"tx:{code}:{group}"))
        self.code, self.group = code, group

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match['code'], match['g'])

    async def callback(self, interaction: discord.Interaction):
        row = live_get(interaction.message.id)
        allowed = row is None or row['started_by'] == interaction.user.id \
            or interaction.permissions.manage_messages
        await interaction.response.defer()
        if not allowed and too_soon(('live', interaction.user.id)):
            await reply_error(interaction, TOO_SOON)
            return
        try:
            b = await fetch_board(self.code, self.group, None if allowed else interaction.user.id)
        except (NotFoundError, ValidationError) as e:
            live_drop(interaction.message.id)
            if row:
                state = 'gone' if isinstance(e, NotFoundError) else 'nogroup'
                await interaction.edit_original_response(view=notice_view(json.loads(row['board']), state))
            else:
                await reply_error(interaction, e.user_message)
            return
        except ServiceDownError as e:
            await reply_error(interaction, e.user_message)
            return
        if not allowed and not is_operator(b):
            await reply_error(interaction, "자동 갱신은 켠 사람이나 이 대회의 운영자가 끌 수 있습니다.")
            return
        live_drop(interaction.message.id)
        await interaction.edit_original_response(view=notice_view(b, 'off'))
        logger.info(f"자동 갱신 끔 {self.code} {self.group}")


async def post_round(channel, b: dict, png: bytes, image_id: Optional[int]) -> int:
    """image_id 메시지를 새 이미지로 고침. 없거나 지워졌으면 새로 올림"""
    view = round_view(b, png)
    if image_id:
        try:
            await channel.get_partial_message(image_id).edit(view=view, attachments=visual.files_of(view))
            return image_id
        except discord.NotFound:
            pass
    return (await channel.send(view=view, files=visual.files_of(view))).id


class Tournament(commands.Cog):
    def __init__(self, client: ERClient):
        self.client = client

    async def cog_load(self):
        self.client.add_dynamic_items(RefreshButton, GroupSelect, LiveButton, LiveOffButton)
        self.live.start()

    async def cog_unload(self):
        self.live.cancel()

    @tasks.loop(seconds=LIVE_SECONDS)
    async def live(self):
        try:
            rows = db.execute("SELECT * FROM live_boards").fetchall()
        except sqlite3.Error as e:
            logger.warning(f"자동 갱신 목록 조회 실패: {e}")
            return
        limit = asyncio.Semaphore(5)

        async def one(row: sqlite3.Row):
            async with limit:
                await self.tick(row)

        await asyncio.gather(*(one(r) for r in rows))

    async def tick(self, row: sqlite3.Row):
        mid = row['message_id']
        channel = self.client.get_partial_messageable(row['channel_id'])
        notice = channel.get_partial_message(mid)
        image_id, last_round = row['image_id'], row['last_round']
        try:
            try:
                b = await fetch_board(row['code'], row['grp'], since=row['version'])
                # 라운드를 모두 지웠으면 마지막 이미지만 빈 순위로 고침
                png = await fetch_image(row['code'], row['grp']) \
                    if b is not UNCHANGED and (b['played'] or image_id) else None
            except (NotFoundError, ValidationError) as e:
                if live_get(mid):
                    live_drop(mid)
                    state = 'gone' if isinstance(e, NotFoundError) else 'nogroup'
                    await notice.edit(view=notice_view(json.loads(row['board']), state))
                return
            except ServiceDownError as e:
                logger.warning(f"자동 갱신 조회 실패 {row['code']}: {e.message}")
                return
            # 조회하는 사이 끄기를 눌렀으면 아무것도 올리지 않음
            if b is UNCHANGED or not live_get(mid):
                return
            if png is not None:
                image_id = await post_round(channel, b, png, None if b['played'] > last_round else image_id)
                last_round = b['played']
            if b['ended']:
                live_drop(mid)
                await notice.edit(view=notice_view(b, 'ended'))
            else:
                live_save(mid, b, image_id, last_round)
        except discord.NotFound:
            live_drop(mid)
            logger.info(f"자동 갱신 안내 메시지 없음, 끔 {row['code']} {mid}")
        except discord.Forbidden:
            # 권한이 돌아오면 이어서 올림
            logger.info(f"자동 갱신 권한 없음 {row['code']} {mid}")
        except discord.HTTPException as e:
            logger.warning(f"자동 갱신 메시지 실패 {row['code']}: {type(e).__name__}: {e}")
        except Exception:
            logger.error(f"자동 갱신 처리 실패 {row['code']} {mid}", exc_info=True)

    @live.before_loop
    async def _before_live(self):
        await self.client.wait_until_ready()

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        # tick은 안내 메시지를 끝날 때만 고쳐서 지워진 것을 따로 알아야 함
        live_drop(payload.message_id)

    async def code_autocomplete(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        rows = await my_tournaments(interaction.user.id)
        q = (current or '').strip().lower()
        choices = []
        for t in rows:
            if q and q not in t['name'].lower() and q not in t['code'].lower():
                continue
            state = f"{t['played']}라운드 진행" if t['played'] else "결과 없음"
            role = "개최자" if t['role'] == 'owner' else "운영자"
            name = f"{t['name']} | {t['code']} | {role} | {state}"
            choices.append(app_commands.Choice(name=name[:100], value=t['code']))
        return choices[:25]

    @app_commands.command(name="대회", description="Labs 대회 순위")
    @app_commands.describe(코드="대회 코드나 Labs 대회 주소", 조="여러 조로 나눈 대회의 조")
    @app_commands.choices(조=[app_commands.Choice(name=f"{g}조", value=g) for g in GROUPS])
    # Labs 봇 토큰 한도(분당 1200회)를 모든 서버가 나눠 씀
    @app_commands.checks.cooldown(3, 10)
    @handle_errors(user_message="대회 순위를 가져오는 중 오류가 발생했습니다.\n잠시 후 다시 시도해주세요.")
    async def tournament_command(self, interaction: discord.Interaction, 코드: str,
                                 조: Optional[app_commands.Choice[str]] = None):
        target = parse_target(코드)
        if not target:
            raise ValidationError(f"대회 코드 형식 {코드[:40]!r}", BAD_CODE)
        code, group = target
        group = 조.value if 조 else group or 'A'
        channel = interaction.channel
        in_guild = interaction.guild is not None and channel is not None and code != 'EXAMPLE'
        live: dict = {}

        async def build():
            b = await fetch_board(code, group, interaction.user.id if in_guild else None)
            if not (in_guild and is_operator(b) and not b['ended']):
                return card_view(b, await fetch_image(code, b['group']))
            if live_running(channel.id, code, b['group']):
                raise ValidationError(f"이미 자동 갱신 중 {code} {b['group']}", DUPLICATE)
            if reason := live_blocked(interaction):
                live['blocked'] = reason
                return card_view(b, await fetch_image(code, b['group']))
            live['board'] = b
            live['png'] = await fetch_image(code, b['group']) if b['played'] else None
            return notice_view(b)

        view, message = await send_card(interaction, "대회 순위 조회 중", build)
        if live.get('blocked'):
            await reply_error(interaction, live['blocked'])
        if view is None or 'board' not in live:
            return
        b, image_id = live['board'], None
        if live['png']:
            try:
                image_id = await post_round(channel, b, live['png'], None)
            except discord.HTTPException as e:
                # 다음 tick이 다시 올림
                logger.warning(f"자동 갱신 첫 이미지 실패 {code}: {type(e).__name__}: {e}")
        live_put(message, interaction.guild.id, b, interaction.user.id, image_id, b['played'] if image_id else 0)
        logger.info(f"자동 갱신 켬 {code} {b['group']} 서버 {interaction.guild.id}")

    tournament_command.autocomplete('코드')(code_autocomplete)


async def setup(client: ERClient):
    await client.add_cog(Tournament(client))
