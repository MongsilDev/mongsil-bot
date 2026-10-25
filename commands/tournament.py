"""
Labs 점수 계산기 대회 순위 이미지. 순위 계산과 그림은 Labs가 함
"""
import asyncio
import base64
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

# 결과 받기. Labs와 같게 채널 하나는 대회 하나의 조 하나에만 연결
db.executescript("""
CREATE TABLE IF NOT EXISTS result_links (
    channel_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    code TEXT NOT NULL,
    grp TEXT NOT NULL,
    saved_at INTEGER NOT NULL
);
""")

RELAY_MAX_BYTES = 200 * 1024
RELAY_RETRY = (1, 3)
RELAY_KEEP_SECONDS = 600
RELAY_KEEP_MAX = 200
NOT_CSV = "게임 결과 CSV가 아닙니다."
LINK_FORBIDDEN = "이 대회의 개최자나 운영자만 연결할 수 있습니다."
UPLOAD_FORBIDDEN = "이 대회의 운영자만 올릴 수 있습니다."
REUPLOAD = "파일을 다시 올려주세요."
RELAY_DOWN = "Labs에 연결하지 못했습니다."
RELAY_STATUSES = ('added', 'duplicate', 'ended', 'invalid', 'forbidden', 'unlinked')


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


async def put_links(code: str, actor: int, links: list) -> list:
    """조와 채널 연결 저장. 응답은 대회의 현재 연결 전체"""
    token = os.getenv('LABS_BOT_TOKEN')
    if not token:
        raise ServiceDownError("LABS_BOT_TOKEN 없음", DOWN)
    session = await api_client.get_session()
    try:
        async with session.put(f"{API}/t/{code}/links", json={'actor': str(actor), 'links': links},
                               headers={'x-bot-token': token}, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status == 403:
                raise ValidationError(f"연결 권한 없음 {code}", LINK_FORBIDDEN)
            if r.status == 404:
                raise NotFoundError(f"대회 없음 {code}", GONE)
            if r.status == 400:
                raise ValidationError(f"없는 조 {code}", "이 대회에 없는 조입니다.")
            if r.status != 200:
                raise ServiceDownError(f"Labs 응답 {r.status}", DOWN)
            return (await r.json())['links']
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as e:
        raise ServiceDownError(f"Labs 연결 실패: {type(e).__name__}", DOWN) from e


async def relay(channel_id: int, actor: int, file_name: str, data: bytes) -> Optional[dict]:
    """게임 결과 CSV를 Labs로 보냄. 1초, 3초 뒤 다시 보내고 그래도 실패하면 None"""
    token = os.getenv('LABS_BOT_TOKEN')
    if not token:
        logger.warning("결과 전달 실패: LABS_BOT_TOKEN 없음")
        return None
    body = {'channelId': str(channel_id), 'actor': str(actor), 'fileName': file_name,
            'data': base64.b64encode(data).decode()}
    for wait in (0, *RELAY_RETRY):
        await asyncio.sleep(wait)
        try:
            session = await api_client.get_session()
            async with session.post(f"{API}/relay", json=body, headers={'x-bot-token': token},
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    result = await r.json()
                    if isinstance(result, dict) and result.get('status') in RELAY_STATUSES:
                        return result
                reason = f"응답 {r.status}"
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            reason = type(e).__name__
        logger.warning(f"결과 전달 Labs 오류: {reason}")
    return None


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
    row = [LiveOffButton(b['code'], b['group']), RelayLinkButton(b['code'])] if state == 'on' else []
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


def card_view(b: dict, png: bytes, relay_button: bool = False) -> ui.LayoutView:
    """자동 갱신 없이 한 번 보는 카드. relay_button은 개수 제한 때문에 운영자에게 보낸 카드"""
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
    row = [RelayLinkButton(b['code'])] if relay_button else []
    view.add_item(ui.ActionRow(RefreshButton(b['code'], b['group']), *row, link_button(b)))
    return view


def result_view(r: dict) -> ui.LayoutView:
    """relay 응답 added, duplicate, ended"""
    if r['status'] == 'added':
        title = r['name'] + (f" {r['group']}조" if r['groupCount'] > 1 else '') + f" {r['round']}라운드"
        lines = [f"### {discord.utils.escape_markdown(title)}"]
        lines += [f"{t['rank']}위 {discord.utils.escape_markdown(t['team'])} {t['score']}점" for t in r['ranking']]
        text = '\n'.join(lines)
    else:
        text = discord.utils.escape_markdown(r['message'])
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(ui.TextDisplay(text), accent_colour=visual.colour('tournament')))
    view.add_item(ui.ActionRow(ui.Button(style=discord.ButtonStyle.link, label="Labs에서 보기",
                                         emoji=EMOJIS['labs'], url=r['url'])))
    return view


def has_relay_button(components) -> bool:
    for c in components or []:
        if c is None:
            continue
        if (getattr(c, 'custom_id', None) or '').startswith('tl:'):
            return True
        if has_relay_button(getattr(c, 'children', None)) or has_relay_button([getattr(c, 'accessory', None)]):
            return True
    return False


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
        view = card_view(b, await fetch_image(code, b['group']), has_relay_button(interaction.message.components))
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


# 결과 받기 연결. linked는 연결된 채널 ID로, 연결 안 된 채널의 메시지는 DB도 보지 않고 넘김

def load_linked() -> set:
    return {r[0] for r in db.execute("SELECT channel_id FROM result_links")}


linked = load_linked()


def link_rows(code: str) -> Dict[str, sqlite3.Row]:
    return {r['grp']: r for r in db.execute("SELECT * FROM result_links WHERE code = ?", (code,))}


def recent_link(guild_id: int, group: str) -> Optional[sqlite3.Row]:
    return db.execute("SELECT * FROM result_links WHERE guild_id = ? AND grp = ? ORDER BY saved_at DESC LIMIT 1",
                      (guild_id, group)).fetchone()


def replace_links(code: str, rows: List[Tuple[int, int, str]]) -> None:
    """대회 코드의 연결을 Labs 응답으로 바꿈. rows는 (채널 ID, 서버 ID, 조)"""
    global linked
    now = int(time.time())
    db.execute("BEGIN")
    try:
        db.execute("DELETE FROM result_links WHERE code = ?", (code,))
        db.executemany("INSERT OR REPLACE INTO result_links (channel_id, guild_id, code, grp, saved_at) "
                       "VALUES (?, ?, ?, ?, ?)", [(c, g, code, grp, now) for c, g, grp in rows])
        db.execute("COMMIT")
    except sqlite3.Error:
        db.execute("ROLLBACK")
        raise
    linked = load_linked()


def unlink(channel_id: int) -> None:
    db.execute("DELETE FROM result_links WHERE channel_id = ?", (channel_id,))
    linked.discard(channel_id)


class LinkSelect(ui.ChannelSelect):
    """미리 고른 채널은 values에 들어오지 않아 고른 채널을 따로 들고 있음"""
    def __init__(self, channel: Optional[discord.abc.GuildChannel]):
        super().__init__(channel_types=[discord.ChannelType.text], min_values=0, max_values=1,
                         default_values=[channel] if channel else [])
        self.chosen: Optional[int] = channel.id if channel else None

    async def callback(self, interaction: discord.Interaction):
        self.chosen = self.values[0].id if self.values else None
        await interaction.response.defer()


class LinkView(ui.LayoutView):
    """조마다 결과를 받을 채널을 고르는 나만 보이는 카드"""
    def __init__(self, code: str, groups: List[str], guild: discord.Guild):
        super().__init__(timeout=600)
        self.code, self.guild = code, guild
        self.rows = link_rows(code)
        self.selects: Dict[str, LinkSelect] = {}
        children = []
        for g in groups:
            row = self.rows.get(g)
            if not row or row['guild_id'] != guild.id:
                row = None if row else recent_link(guild.id, g)
            channel = guild.get_channel(row['channel_id']) if row else None
            select = LinkSelect(channel)
            self.selects[g] = select
            if len(groups) > 1:
                children.append(ui.TextDisplay(f"{g}조"))
            children.append(ui.ActionRow(select))
        self.add_item(ui.Container(*children, accent_colour=visual.colour('tournament')))
        save = ui.Button(label="저장", style=discord.ButtonStyle.primary)
        save.callback = self.save
        self.add_item(ui.ActionRow(save))

    async def save(self, interaction: discord.Interaction):
        await interaction.response.defer()
        me = self.guild.me
        chosen: Dict[str, Optional[discord.abc.GuildChannel]] = {}
        missing_lines = []
        for g, select in self.selects.items():
            channel = self.guild.get_channel(select.chosen) if select.chosen else None
            chosen[g] = channel
            if channel is not None:
                perms = channel.permissions_for(me)
                # 결과는 답장으로 보내므로 메시지 기록 보기도 필요
                missing = [n for n, ok in (('채널 보기', perms.view_channel), ('메시지 보내기', perms.send_messages),
                                           ('메시지 기록 보기', perms.read_message_history)) if not ok]
                if missing:
                    name = discord.utils.escape_markdown(channel.name)
                    missing_lines.append(f"몽실봇에 #{name}의 {', '.join(missing)} 권한이 없습니다.")
        if missing_lines:
            missing_lines.append("채널 권한에서 몽실봇에 이 권한을 허용해주세요.")
            await reply_error(interaction, '\n'.join(missing_lines))
            return
        # 다른 서버에 연결된 조를 비워 두었으면 그 연결은 건드리지 않음
        links = []
        for g, channel in chosen.items():
            row = self.rows.get(g)
            if channel is None and row and row['guild_id'] != self.guild.id:
                continue
            links.append({'group': g, 'channelId': str(channel.id) if channel else None,
                          'guild': self.guild.name, 'channel': channel.name if channel else ''})
        try:
            current = await put_links(self.code, interaction.user.id, links)
        except (NotFoundError, ServiceDownError, ValidationError) as e:
            await reply_error(interaction, e.user_message)
            return
        rows = []
        for link in current:
            if not link.get('channelId'):
                continue
            cid = int(link['channelId'])
            channel = interaction.client.get_channel(cid)
            old = next((r for r in self.rows.values() if r['channel_id'] == cid), None)
            guild_id = channel.guild.id if channel else old['guild_id'] if old else self.guild.id
            rows.append((cid, guild_id, link['group']))
        replace_links(self.code, rows)
        self.stop()
        done = ui.LayoutView(timeout=None)
        done.add_item(ui.Container(ui.TextDisplay("저장했습니다."), accent_colour=visual.colour('tournament')))
        await interaction.edit_original_response(view=done)
        logger.info(f"결과 받기 연결 {self.code} {','.join(r[2] for r in rows) or '없음'}")


class RelayLinkButton(ui.DynamicItem[ui.Button], template=r'tl:(?P<code>[0-9A-Z]{6,7})'):
    def __init__(self, code: str):
        super().__init__(ui.Button(label="결과 받기", style=discord.ButtonStyle.secondary, custom_id=f"tl:{code}"))
        self.code = code

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match['code'])

    async def callback(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return await reply_error(interaction, LINK_FORBIDDEN)
        if too_soon(('link', interaction.user.id)):
            return await reply_error(interaction, TOO_SOON)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            b = await fetch_board(self.code, None, interaction.user.id)
        except (NotFoundError, ServiceDownError, ValidationError) as e:
            return await reply_error(interaction, e.user_message)
        if not is_operator(b):
            return await reply_error(interaction, LINK_FORBIDDEN)
        groups = b.get('groups') or list(GROUPS[:b['groupCount']])
        await interaction.followup.send(view=LinkView(self.code, groups, interaction.guild), ephemeral=True)


_kept: Dict[int, Tuple[float, int, str, bytes]] = {}


def keep_file(key: int, channel_id: int, author_id: int, file_name: str, data: bytes) -> None:
    """다시 보내기용 파일 바이트. RELAY_KEEP_SECONDS 동안 메모리에만"""
    now = time.monotonic()
    for k in [k for k, v in _kept.items() if v[0] < now]:
        del _kept[k]
    while len(_kept) >= RELAY_KEEP_MAX:
        del _kept[next(iter(_kept))]
    _kept[key] = (now + RELAY_KEEP_SECONDS, channel_id, author_id, file_name, data)


def retry_view(key: int) -> ui.LayoutView:
    view = create_error_layout(RELAY_DOWN)
    view.add_item(ui.ActionRow(RelayRetryButton(key)))
    return view


def log_result(r: dict) -> None:
    logger.info(f"결과 전달 {r.get('code') or '-'} {r.get('group') or '-'} {r['status']}")


class RelayRetryButton(ui.DynamicItem[ui.Button], template=r'tq:(?P<key>[0-9]+)'):
    def __init__(self, key: int):
        super().__init__(ui.Button(label="다시 보내기", style=discord.ButtonStyle.secondary, custom_id=f"tq:{key}"))
        self.key = key

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match['key']))

    async def callback(self, interaction: discord.Interaction):
        kept = _kept.get(self.key)
        if not kept or kept[0] < time.monotonic():
            _kept.pop(self.key, None)
            return await reply_error(interaction, REUPLOAD)
        if too_soon(('relay', interaction.user.id)):
            return await reply_error(interaction, TOO_SOON)
        await interaction.response.defer()
        # 동시에 눌러도 한 번만 보내도록 먼저 꺼내고, 실패하면 되돌림. 권한은 누른 사람이 아닌 올린 사람 기준
        _kept.pop(self.key, None)
        _, channel_id, author_id, file_name, data = kept
        r = await relay(channel_id, author_id, file_name, data)
        if r is None:
            _kept[self.key] = kept
            return await reply_error(interaction, RELAY_DOWN)
        log_result(r)
        status = r['status']
        if status == 'forbidden':
            await interaction.edit_original_response(view=create_error_layout(UPLOAD_FORBIDDEN))
        elif status == 'unlinked':
            unlink(channel_id)
            await interaction.message.delete()
        elif status == 'invalid':
            await interaction.edit_original_response(view=create_error_layout(NOT_CSV))
        else:
            await interaction.edit_original_response(view=result_view(r))


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
        self.client.add_dynamic_items(RefreshButton, GroupSelect, LiveButton, LiveOffButton, RelayLinkButton,
                                      RelayRetryButton)
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
                new_round = b['played'] > last_round
                image_id = await post_round(channel, b, png, None if new_round else image_id)
                last_round = b['played']
            if b['ended']:
                live_drop(mid)
                await notice.edit(view=notice_view(b, 'ended'))
            else:
                live_save(mid, b, image_id, last_round)
                # 안내 메시지의 라운드 수를 맞추고, 봇이 꺼진 사이 지워졌으면 NotFound로 끔
                if png is not None and new_round:
                    await notice.edit(view=notice_view(b))
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

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        for mid in payload.message_ids:
            live_drop(mid)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot or message.channel.id not in linked:
            return
        files = [a for a in message.attachments if a.filename.lower().endswith('.csv')]
        if not files:
            return
        # 첨부 주소는 만료 시각이 서명돼 있어 Labs로 보내기 전에 모두 내려받음
        loaded = []
        for a in files:
            if a.size > RELAY_MAX_BYTES:
                loaded.append((a, None))
                continue
            try:
                loaded.append((a, await a.read()))
            except discord.HTTPException as e:
                logger.warning(f"결과 파일 내려받기 실패: {type(e).__name__}")
                try:
                    await message.reply(view=create_error_layout(REUPLOAD), mention_author=False)
                except discord.HTTPException:
                    pass
        for a, data in loaded:
            try:
                await self.relay_one(message, a, data)
            except discord.HTTPException as e:
                logger.warning(f"결과 전달 답장 실패: {type(e).__name__}: {e}")
            except Exception:
                logger.error("결과 전달 처리 실패", exc_info=True)
            if message.channel.id not in linked:
                return

    async def relay_one(self, message: discord.Message, a: discord.Attachment, data: Optional[bytes]):
        def reply(view, **kwargs):
            return message.reply(view=view, mention_author=False, **kwargs)

        if data is None:
            await reply(create_error_layout(NOT_CSV))
            return
        r = await relay(message.channel.id, message.author.id, a.filename, data)
        if r is None:
            keep_file(a.id, message.channel.id, message.author.id, a.filename, data)
            await reply(retry_view(a.id))
            return
        log_result(r)
        status = r['status']
        if status == 'unlinked':
            unlink(message.channel.id)
        elif status == 'invalid':
            await reply(create_error_layout(NOT_CSV))
        elif status == 'forbidden':
            await reply(create_error_layout(UPLOAD_FORBIDDEN), delete_after=30)
        else:
            await reply(result_view(r))

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
                return card_view(b, await fetch_image(code, b['group']), True)
            live['board'] = b
            live['png'] = await fetch_image(code, b['group']) if b['played'] else None
            return notice_view(b)

        view, message = await send_card(interaction, "대회 순위 조회 중", build)
        if live.get('blocked'):
            await reply_error(interaction, live['blocked'])
        if view is None or 'board' not in live:
            return
        b = live['board']
        # 첫 이미지를 올리는 사이 끄기를 누를 수 있어 기록부터 남김. version 0이면 다음 tick이 since 없이 조회해 다시 올림
        live_put(message, interaction.guild.id, {**b, 'version': 0}, interaction.user.id, None, 0)
        if live['png']:
            try:
                image_id = await post_round(channel, b, live['png'], None)
            except discord.HTTPException as e:
                logger.warning(f"자동 갱신 첫 이미지 실패 {code}: {type(e).__name__}: {e}")
            else:
                if live_get(message.id):
                    live_save(message.id, b, image_id, b['played'])
        logger.info(f"자동 갱신 켬 {code} {b['group']} 서버 {interaction.guild.id}")

    tournament_command.autocomplete('코드')(code_autocomplete)


async def setup(client: ERClient):
    await client.add_cog(Tournament(client))
