"""
Labs 점수 계산기 대회 순위 카드. 순위 계산은 Labs가 하고 봇은 받은 값을 그림
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
TIE = {'killScore': '킬 점수로 가름', 'kills': '킬 수로 가름', 'rankScore': '순위 점수로 가름',
       'wins': '1위 횟수로 가름', 'lastPlacement': '마지막 라운드 순위로 가름'}
NAME_MAX = 20

LIVE_SECONDS = 30
LIVE_HOURS = 6
LIVE_PER_GUILD = 3
LIVE_PER_USER = 3
LIVE_TOTAL = 50
REFRESH_GAP = 5.0
TOO_SOON = "잠시 후 다시 눌러주세요."

BAD_CODE = "대회 코드는 영문과 숫자 6자리입니다.\n-# Labs 대회 화면의 코드나 주소를 그대로 붙여 넣어도 됩니다."
GONE = "없거나 지워진 대회입니다.\n-# 대회는 마지막 수정 뒤 24시간이 지나면 지워집니다."
DOWN = "Labs에 연결하지 못했습니다.\n잠시 후 다시 시도해주세요."

UNCHANGED = object()
# 버튼 없이 그리는 상태
STOPPED = ('gone', 'nogroup')

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
    board TEXT NOT NULL
);
""")


def parse_target(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """대회 코드와 조. Labs 주소를 붙여 넣으면 주소에서 꺼냄"""
    text = (text or '').strip()
    m = LINK.search(text)
    if m:
        code = 'EXAMPLE' if m[2] else m[1].upper()
        return (code, m[3].upper() if m[3] else None) if CODE.match(code) else None
    code = re.sub(r'\s+', '', text).upper()
    return (code, None) if CODE.match(code) else None


async def fetch_board(code: str, group: Optional[str] = None, user_id: Optional[int] = None,
                      since: Optional[int] = None):
    """Labs가 계산한 조 순위. 바뀐 게 없으면 UNCHANGED"""
    token = os.getenv('LABS_BOT_TOKEN')
    if not token:
        raise ServiceDownError("LABS_BOT_TOKEN 없음", DOWN)
    params = {k: str(v) for k, v in (('group', group), ('user', user_id), ('since', since)) if v}
    session = await api_client.get_session()
    try:
        async with session.get(f"{API}/t/{code}", params=params, headers={'x-bot-token': token},
                               timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status == 204:
                return UNCHANGED
            if r.status == 404:
                raise NotFoundError(f"대회 없음 {code}", GONE)
            if r.status == 400:
                raise ValidationError(f"없는 조 {code} {group}", f"이 대회에는 {group}조가 없습니다.")
            if r.status != 200:
                raise ServiceDownError(f"Labs 응답 {r.status}", DOWN)
            return await r.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        raise ServiceDownError(f"Labs 연결 실패: {type(e).__name__}", DOWN) from e


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


# 카드

def num(v: float) -> str:
    return f'{v:g}'


def clip(name: str) -> str:
    name = name if len(name) <= NAME_MAX else name[:NAME_MAX - 2] + '..'
    return discord.utils.escape_markdown(name)


def team_line(s: dict, b: dict) -> str:
    delta = s['delta']
    move = f"  ▲{delta}" if delta > 0 else f"  ▼{-delta}" if delta < 0 else ""
    line = f"`{s['rank']:>2}` **{clip(s['name'])}**  {num(s['total'])}점{move}"
    detail = [f"킬 {s['kills']}" if b['tiebreak'] == 'kills' else f"킬 점수 {num(s['killScore'])}"]
    if b['ended'] and s['rank'] == 1 and b['mode'] != 'points':
        detail.append('우승')
    elif s.get('fixed'):
        detail.append(f"{s['fixed']}위 확정")
    elif b['mode'] != 'points' and s.get('checkpoint') and not b['ended']:
        detail.append('체크포인트')
    if s.get('penalty'):
        detail.append(f"감점 {num(s['penalty'])}")
    if s.get('tiedBy') in TIE:
        detail.append(f"동점, {TIE[s['tiedBy']]}")
    return line + '\n-# ' + ' | '.join(detail)


def sub_line(b: dict) -> str:
    parts = [MODE.get(b['mode'], b['mode'])]
    if b['mode'] == 'points':
        parts.append(f"{b['plannedRounds']}라운드 종료" if b['ended'] else f"{b['played']}/{b['plannedRounds']}라운드")
    elif b['ended']:
        parts.append(f"{b['played']}라운드에서 종료")
    else:
        parts.append(f"{b['played']}라운드 진행, 최대 {b['plannedRounds']}라운드")
    if b['mode'] == 'points' and b.get('advance'):
        parts.append(f"상위 {b['advance']}팀 진출")
    return '-# ' + ' | '.join(parts)


def next_lines(n: dict) -> List[str]:
    weather = ', '.join(w for w in (n.get('main'), n.get('sub')) if w)
    lines = [f"-# 다음 {n['roundNo']}라운드" + (f" | 날씨 {weather}" if weather else '')]
    if n.get('bans'):
        lines.append('-# 금지 실험체 ' + ', '.join(n['bans']))
    return lines


def board_view(b: dict, live: Optional[str] = None) -> ui.LayoutView:
    """live: None, 'on', 'ended', 'expired', 'gone', 'nogroup'"""
    title = b['name'] + (f" {b['group']}조" if b['groupCount'] > 1 else '')
    children = [ui.TextDisplay(f"### {discord.utils.escape_markdown(title)}\n{sub_line(b)}"), ui.Separator()]
    st = b['standings']
    if not st:
        children.append(ui.TextDisplay("아직 등록된 경기 결과가 없습니다."))
    else:
        cut = b['advance'] if b['mode'] == 'points' and 0 < b.get('advance', 0) < len(st) else 0
        head, tail = (st[:cut], st[cut:]) if cut else (st, [])
        children.append(ui.TextDisplay('\n'.join(team_line(s, b) for s in head)))
        if tail:
            children += [ui.Separator(), ui.TextDisplay('\n'.join(team_line(s, b) for s in tail))]

    foot = next_lines(b['next']) if b.get('next') and not b['ended'] and live not in STOPPED else []
    edited = f"마지막 수정 <t:{b['updatedAt'] // 1000}:R>" if b.get('updatedAt') else ''
    if live == 'on':
        foot.append('-# ' + ' | '.join(p for p in ('자동 갱신 중', edited) if p))
    elif live == 'ended':
        foot.append('-# 대회가 끝나 자동 갱신을 멈췄습니다.')
    elif live == 'expired':
        foot.append(f'-# {LIVE_HOURS}시간이 지나 자동 갱신을 멈췄습니다.')
    elif live == 'gone':
        foot.append('-# 대회가 지워져 자동 갱신을 멈췄습니다.')
    elif live == 'nogroup':
        foot.append(f"-# {b['group']}조가 없어져 자동 갱신을 멈췄습니다.")
    elif edited:
        foot.append('-# ' + edited)
    if foot:
        children += [ui.Separator(visible=False), ui.TextDisplay('\n'.join(foot))]

    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*children, accent_colour=visual.colour('tournament')))
    if live in STOPPED:
        return view
    if b['groupCount'] > 1:
        view.add_item(ui.ActionRow(GroupSelect(b['code'], b['groups'], b['group'])))
    row = [RefreshButton(b['code'], b['group']),
           ui.Button(style=discord.ButtonStyle.link, label="순위표", emoji=EMOJIS['labs'], url=b['url'])]
    if live == 'on':
        row.append(LiveOffButton(b['code'], b['group']))
    elif not b['ended'] and not live and b['code'] != 'EXAMPLE':
        row.append(LiveButton(b['code'], b['group']))
    view.add_item(ui.ActionRow(*row))
    return view


# 자동 갱신 기록

def live_get(message_id: int) -> Optional[sqlite3.Row]:
    return db.execute("SELECT * FROM live_boards WHERE message_id = ?", (message_id,)).fetchone()


def live_put(message: discord.Message, guild_id: int, b: dict, user_id: int) -> None:
    db.execute("INSERT OR REPLACE INTO live_boards VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (message.id, message.channel.id, guild_id, b['code'], b['group'], b['version'], user_id,
                int(time.time()), json.dumps(b, ensure_ascii=False)))


def live_save(message_id: int, b: dict) -> None:
    db.execute("UPDATE live_boards SET grp = ?, version = ?, board = ? WHERE message_id = ?",
               (b['group'], b['version'], json.dumps(b, ensure_ascii=False), message_id))


def live_drop(message_id: int) -> None:
    db.execute("DELETE FROM live_boards WHERE message_id = ?", (message_id,))


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
    """새로고침과 조 선택은 메시지마다, 자동 갱신 켜기와 끄기는 사람마다 REFRESH_GAP 간격"""
    now = time.monotonic()
    if now - _last_refresh.get(key, 0) < REFRESH_GAP:
        return True
    _last_refresh[key] = now
    if len(_last_refresh) > 5000:
        _last_refresh.clear()
    return False


def live_limit(guild_id: int, user_id: int, message_id: int) -> Optional[str]:
    rows = db.execute("SELECT guild_id, started_by FROM live_boards WHERE message_id != ?", (message_id,)).fetchall()
    if sum(r['guild_id'] == guild_id for r in rows) >= LIVE_PER_GUILD:
        return (f"이 서버에서는 자동 갱신을 {LIVE_PER_GUILD}개까지 켤 수 있습니다.\n"
                "다른 카드의 자동 갱신을 끈 뒤 다시 시도해주세요.")
    if sum(r['started_by'] == user_id for r in rows) >= LIVE_PER_USER:
        return (f"자동 갱신은 한 사람이 {LIVE_PER_USER}개까지 켤 수 있습니다.\n"
                "다른 카드의 자동 갱신을 끈 뒤 다시 시도해주세요.")
    if len(rows) >= LIVE_TOTAL:
        return "지금은 자동 갱신을 더 켤 수 없습니다.\n잠시 후 다시 시도해주세요."
    return None


async def redraw(interaction: discord.Interaction, code: str, group: str, user_id: Optional[int] = None):
    """카드를 새 값으로 다시 그림. 자동 갱신 중인 카드면 기록도 갱신"""
    await interaction.response.defer()
    try:
        b = await fetch_board(code, group, user_id)
    except (NotFoundError, ServiceDownError, ValidationError) as e:
        await reply_error(interaction, e.user_message)
        return None
    row = live_get(interaction.message.id)
    live = None
    if row:
        if b['ended']:
            live_drop(interaction.message.id)
            live = 'ended'
        else:
            live_save(interaction.message.id, b)
            live = 'on'
    await interaction.edit_original_response(view=board_view(b, live))
    return b


class RefreshButton(ui.DynamicItem[ui.Button], template=r'tr:(?P<code>[0-9A-Z]{6,7}):(?P<g>[A-H])'):
    def __init__(self, code: str, group: str):
        super().__init__(ui.Button(label="새로고침", emoji="🔄", style=discord.ButtonStyle.secondary,
                                   custom_id=f"tr:{code}:{group}"))
        self.code, self.group = code, group

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match['code'], match['g'])

    async def callback(self, interaction: discord.Interaction):
        if too_soon(interaction.message.id):
            await interaction.response.defer()
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
        group = self.item.values[0]
        row = live_get(interaction.message.id)
        if row and row['grp'] != group and row['started_by'] != interaction.user.id \
                and not interaction.permissions.manage_messages:
            await reply_error(interaction, "자동 갱신 중인 카드는 자동 갱신을 켠 사람만 조를 바꿀 수 있습니다.")
            return
        if too_soon(interaction.message.id):
            await interaction.response.defer()
            return
        await redraw(interaction, self.code, group)


class LiveButton(ui.DynamicItem[ui.Button], template=r'ta:(?P<code>[0-9A-Z]{6,7}):(?P<g>[A-H])'):
    def __init__(self, code: str, group: str):
        super().__init__(ui.Button(label="자동 갱신", style=discord.ButtonStyle.secondary,
                                   custom_id=f"ta:{code}:{group}"))
        self.code, self.group = code, group

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match['code'], match['g'])

    async def callback(self, interaction: discord.Interaction):
        guild, channel = interaction.guild, interaction.channel
        if guild is None or channel is None:
            await reply_error(interaction, "자동 갱신은 서버 채널에서만 켤 수 있습니다.")
            return
        if not interaction.app_permissions.view_channel:
            await reply_error(interaction, "몽실봇이 이 채널을 볼 수 없어 자동 갱신을 켤 수 없습니다.\n"
                                           "채널 권한에서 몽실봇에 채널 보기를 허용해주세요.")
            return
        if full := live_limit(guild.id, interaction.user.id, interaction.message.id):
            await reply_error(interaction, full)
            return
        await interaction.response.defer()
        # 권한 판단에 Labs 응답이 필요해 거절될 클릭도 Labs를 부름
        if too_soon(('live', interaction.user.id)):
            await reply_error(interaction, TOO_SOON)
            return
        try:
            b = await fetch_board(self.code, self.group, interaction.user.id)
        except (NotFoundError, ServiceDownError, ValidationError) as e:
            await reply_error(interaction, e.user_message)
            return
        if not is_operator(b):
            await reply_error(interaction, "자동 갱신은 이 대회의 개최자와 운영자만 켤 수 있습니다.")
            return
        if b['ended']:
            await interaction.edit_original_response(view=board_view(b))
            await reply_error(interaction, "끝난 대회는 자동 갱신을 켤 수 없습니다.")
            return
        # fetch를 기다리는 사이 다른 카드가 켜졌을 수 있음
        if full := live_limit(guild.id, interaction.user.id, interaction.message.id):
            await reply_error(interaction, full)
            return
        live_put(interaction.message, guild.id, b, interaction.user.id)
        await interaction.edit_original_response(view=board_view(b, 'on'))
        logger.info(f"자동 갱신 켬 {self.code} {self.group} 서버 {guild.id}")


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
                await interaction.edit_original_response(view=board_view(json.loads(row['board']), state))
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
        await interaction.edit_original_response(view=board_view(b))
        logger.info(f"자동 갱신 끔 {self.code} {self.group}")


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
        message = self.client.get_partial_messageable(row['channel_id']).get_partial_message(mid)
        try:
            if time.time() - row['started_at'] > LIVE_HOURS * 3600:
                live_drop(mid)
                await message.edit(view=board_view(json.loads(row['board']), 'expired'))
                return
            try:
                b = await fetch_board(row['code'], row['grp'], since=row['version'])
            except (NotFoundError, ValidationError) as e:
                if live_get(mid):
                    live_drop(mid)
                    state = 'gone' if isinstance(e, NotFoundError) else 'nogroup'
                    await message.edit(view=board_view(json.loads(row['board']), state))
                return
            except ServiceDownError as e:
                logger.warning(f"자동 갱신 조회 실패 {row['code']}: {e.message}")
                return
            # 조회하는 사이 끄기를 눌렀으면 다시 켜진 카드로 그리지 않음
            if b is UNCHANGED or not live_get(mid):
                return
            if b['ended']:
                live_drop(mid)
                await message.edit(view=board_view(b, 'ended'))
            else:
                live_save(mid, b)
                await message.edit(view=board_view(b, 'on'))
        except discord.NotFound:
            live_drop(mid)
            logger.info(f"자동 갱신 대상 메시지 없음, 끔 {row['code']} {mid}")
        except discord.Forbidden:
            # 권한이 돌아오면 이어서 고침. 계속 막혀 있으면 LIVE_HOURS에 끝남
            logger.info(f"자동 갱신 수정 권한 없음 {row['code']} {mid}")
        except discord.HTTPException as e:
            logger.warning(f"자동 갱신 수정 실패 {row['code']}: {type(e).__name__}: {e}")
        except Exception:
            logger.error(f"자동 갱신 처리 실패 {row['code']} {mid}", exc_info=True)

    @live.before_loop
    async def _before_live(self):
        await self.client.wait_until_ready()

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

        async def build():
            return board_view(await fetch_board(code, group))

        await send_card(interaction, "대회 순위 조회 중", build)

    tournament_command.autocomplete('코드')(code_autocomplete)


async def setup(client: ERClient):
    await client.add_cog(Tournament(client))
