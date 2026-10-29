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
from utils import app_emojis, visual
from utils.api_client import api_client
from utils.config import config
from utils.character_names import CHARACTER_NAMES
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
LINKS_SECONDS = 60
# 다음 라운드 줄이 이 글자 수를 넘으면 금지 실험체를 다음 줄로 내림. 휴대폰 한 줄 기준
NEXT_LINE_WIDTH = 36
# 안내 이미지를 못 받은 보드는 이만큼 지나서 다시 시도
INFO_RETRY_SECONDS = 300
# 대회의 조는 8개까지라 서버 하나에서 모든 조를 켤 수 있게 함. 사람당 한도는 /대회로 켠 보드만 셈
LIVE_PER_GUILD = 8
LIVE_PER_USER = 3
LIVE_TOTAL = 50
REFRESH_GAP = 5.0
TOO_SOON = "잠시 후 다시 눌러주세요."

BAD_CODE = "대회 코드는 영문과 숫자 6자리입니다.\n받은 코드를 다시 확인해주세요."
GONE = "없거나 지워진 대회입니다.\n-# 대회는 마지막으로 수정한 지 24시간이 지나면 자동으로 삭제됩니다."
DOWN = "Labs에 연결하지 못했습니다.\n잠시 후 다시 시도해주세요."
OLD_RELAY_BUTTON = "이 버튼은 더 이상 쓰지 않습니다.\n채널을 연결하려면 이 채널에서 /대회를 다시 입력해주세요."
OLD_OFF_BUTTON = "이 버튼은 더 이상 쓰지 않습니다.\n자동 갱신을 끄려면 Labs 관리 화면에서 연결을 해제해주세요."

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
    last_round INTEGER NOT NULL DEFAULT 0,
    notice TEXT
);
""")
_columns = {r[1] for r in db.execute("PRAGMA table_info(live_boards)")}
if 'image_id' not in _columns:
    db.execute("ALTER TABLE live_boards ADD COLUMN image_id INTEGER")
if 'last_round' not in _columns:
    db.execute("ALTER TABLE live_boards ADD COLUMN last_round INTEGER NOT NULL DEFAULT 0")
# 마지막으로 올린 안내 내용. 배포 전 보드는 비어 있어 첫 tick이 새 형식으로 고침
if 'notice' not in _columns:
    db.execute("ALTER TABLE live_boards ADD COLUMN notice TEXT")

# Labs discord_links의 사본. 채널 하나는 대회 하나의 조 하나에만, 조 하나에는 채널 하나만 연결
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
ACTOR_NAME_MAX = 32
NOT_CSV = "게임 결과 CSV가 아닙니다.\n게임 결과 CSV 파일을 올려주세요."
LINK_FORBIDDEN = "이 대회의 개최자나 운영자만 채널을 연결할 수 있습니다."
DOWNLOAD_FAILED = "파일을 받지 못했습니다.\n같은 파일을 이 채널에 다시 올려주세요."
KEEP_EXPIRED = "보관한 파일이 없어 다시 보낼 수 없습니다.\n같은 파일을 이 채널에 다시 올려주세요."
RELAY_DOWN = "Labs에 연결하지 못했습니다.\n잠시 후 다시 눌러주세요."
RELAY_STATUSES = ('added', 'duplicate', 'ended', 'invalid', 'unlinked')


def no_group(group: Optional[str]) -> str:
    return f"이 대회에는 {group}조가 없습니다.\n조를 다시 골라주세요."


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
                raise ValidationError(f"없는 조 {code} {group}", no_group(group))
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


async def host_name(b: dict, guild: Optional[discord.Guild]) -> str:
    """개최자의 이 서버 표시 이름. 서버에 없으면 Labs에 저장된 디스코드 이름"""
    host = b.get('host')
    if not host:
        return ''
    if guild is not None:
        member = guild.get_member(int(host['id']))
        if member is None:
            try:
                member = await guild.fetch_member(int(host['id']))
            except discord.HTTPException:
                member = None
        if member is not None:
            return member.display_name
    return host['name']


async def fetch_info_image(b: dict, guild: Optional[discord.Guild]) -> bytes:
    """안내 메시지에 둘 대회 정보 PNG. 순위 없이 방식과 개최자만 그림"""
    params = {'group': b['group'], 'info': '1'}
    if name := await host_name(b, guild):
        params['host'] = name
    return await _labs_get(b['code'], '/image', params, b['group'], image=True)


async def _labs_links(method: str, path: str, code: str, group: Optional[str] = None,
                      body: Optional[dict] = None) -> list:
    token = os.getenv('LABS_BOT_TOKEN')
    if not token:
        raise ServiceDownError("LABS_BOT_TOKEN 없음", DOWN)
    session = await api_client.get_session()
    try:
        async with session.request(method, f"{API}{path}", json=body, headers={'x-bot-token': token},
                                   timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status == 403:
                raise ValidationError(f"연결 권한 없음 {code}", LINK_FORBIDDEN)
            if r.status == 404:
                raise NotFoundError(f"대회 없음 {code}", GONE)
            if r.status == 400:
                raise ValidationError(f"없는 조 {code} {group}", no_group(group))
            if r.status != 200:
                raise ServiceDownError(f"Labs 응답 {r.status}", DOWN)
            links = (await r.json())['links']
            if not isinstance(links, list):
                raise TypeError('links')
            return links
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as e:
        raise ServiceDownError(f"Labs 연결 실패: {type(e).__name__}", DOWN) from e


async def put_links(code: str, actor: int, links: list, group: Optional[str] = None) -> list:
    """조와 채널 연결 저장. 응답은 대회의 현재 연결 전체"""
    return await _labs_links('PUT', f"/t/{code}/links", code, group, {'actor': str(actor), 'links': links})


async def fetch_links(code: str) -> list:
    """대회의 현재 연결 전체. 없거나 지워진 대회면 NotFoundError"""
    return await _labs_links('GET', f"/t/{code}/links", code)


async def fetch_all_links() -> list:
    """끝나지 않은 대회의 연결 전체"""
    return await _labs_links('GET', "/links", '-')


async def relay(channel_id: int, actor: int, actor_name: str, file_name: str, data: bytes) -> Optional[dict]:
    """게임 결과 CSV를 Labs로 보냄. 1초, 3초 뒤 다시 보내고 그래도 실패하면 None"""
    token = os.getenv('LABS_BOT_TOKEN')
    if not token:
        logger.warning("결과 전달 실패: LABS_BOT_TOKEN 없음")
        return None
    body = {'channelId': str(channel_id), 'actor': str(actor), 'actorName': actor_name[:ACTOR_NAME_MAX],
            'fileName': file_name, 'data': base64.b64encode(data).decode()}
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

def ban_text(names: List[str]) -> str:
    """Labs는 금지 실험체를 한국어 이름으로 줘 이름으로 코드를 찾아 얼굴 이모지를 붙임.
    얼굴이 하나라도 없으면 띄어쓰기 든 이름끼리 이어져 쉼표로 나눔"""
    faces = [app_emojis.character(next((c for c, n in CHARACTER_NAMES.items() if n == name), 0)) for name in names]
    if all(faces):
        return ' '.join(f"{face} {name}" for face, name in zip(faces, names))
    return ', '.join(f"{face} {name}" if face else name for face, name in zip(faces, names))


def next_lines(n: dict) -> List[str]:
    """다음 라운드의 날씨와 금지 실험체. 둘 다 없으면 라운드 번호는 이미지에 있어 줄을 두지 않음"""
    weather = ', '.join(w for w in (n.get('main'), n.get('sub')) if w)
    bans = n.get('bans') or []
    if not weather and not bans:
        return []
    head = f"-# {n['roundNo']}라운드" + (f" | 날씨 {weather}" if weather else '')
    if not bans:
        return [head]
    # 얼굴 이모지는 화면에서 두 글자 폭
    width = len(head) + 6 + sum(len(name) + 3 for name in bans)
    if width > NEXT_LINE_WIDTH:
        return [head, '-# 금지 ' + ban_text(bans)]
    return [f"{head} | 금지 {ban_text(bans)}"]


def link_button(b: dict) -> ui.Button:
    return ui.Button(style=discord.ButtonStyle.link, label="순위표", url=b['url'])


def gallery(view: ui.LayoutView, png: bytes) -> ui.MediaGallery:
    return ui.MediaGallery(discord.MediaGalleryItem(visual.attach(view, 'tournament.png', png)))


# 멈춘 안내의 한 줄. 끝난 대회는 최종 순위 메시지가 따로 있어 이미지가 있으면 줄을 두지 않음
STOPPED = {
    'gone': "삭제된 대회입니다.",
    'nogroup': "삭제된 조입니다.",
    'unlinked': "이 채널에는 더 이상 순위를 올리지 않습니다.",
    'moved': "이 채널에는 더 이상 순위를 올리지 않습니다.",
    'replaced': "이 채널에는 더 이상 순위를 올리지 않습니다.",
}
# 안내 기록 끝의 표시. INFO는 지금 대회 정보 이미지, IMAGE는 배포 전 이미지나 못 바꾼 옛 이미지
INFO, IMAGE = '\ninfo', '\nimage'


def notice_text(b: dict, state: str = 'on', image: bool = False) -> str:
    """안내 메시지 글. state: 'on'이나 멈춘 이유. image면 대회 이름과 방식은 이미지 머리글에 있어 뺌"""
    lines = []
    if not image:
        title = b['name'] + (f" {b['group']}조" if b['groupCount'] > 1 else '')
        lines.append(f"### {discord.utils.escape_markdown(title)}")
    if state == 'ended':
        return "\n".join(lines if image else [*lines, "-# 경기가 끝났습니다."])
    if state != 'on':
        return '\n'.join([*lines, f"-# {STOPPED[state]}"])
    if not image:
        mode = MODE.get(b['mode'], b['mode'])
        lines.append(f"-# {mode} | {b['plannedRounds']}라운드" if b['mode'] == 'points' else f"-# {mode}")
    # 1라운드 결과가 오기 전에는 라운드 메시지가 없어 다음 라운드를 여기에 둠
    if not b['played'] and b.get('next') and not b['ended']:
        lines += next_lines(b['next'])
    return '\n'.join(lines)


def notice_key(b: dict, mark: str = INFO) -> str:
    """안내 메시지 기록. 이미지에 그리는 이름과 방식이 바뀌어도 달라지게 글만 있는 형식으로 씀.
    mark는 붙은 이미지의 종류이고 글만 있으면 빈 문자열"""
    return f"{notice_text(b)}\n{b['url']}{mark}"


def notice_mark(key: Optional[str]) -> str:
    return next((m for m in (INFO, IMAGE) if key and key.endswith(m)), '')


def has_image(key: Optional[str]) -> bool:
    return bool(notice_mark(key))


def notice_view(b: dict, state: str = 'on', png: Optional[bytes] = None, image: bool = False) -> ui.LayoutView:
    """자동 갱신 안내. state: 'on', 'ended', 'gone', 'nogroup', 'unlinked', 'moved', 'replaced'.
    png는 대회 정보 이미지. png 없이 image면 이미 올린 이미지를 그대로 씀"""
    view = ui.LayoutView(timeout=None)
    image = image or png is not None
    children: list = []
    if image:
        children.append(gallery(view, png) if png is not None
                        else ui.MediaGallery(discord.MediaGalleryItem('attachment://tournament.png')))
    if text := notice_text(b, state, image):
        children.append(ui.TextDisplay(text))
    view.add_item(ui.Container(*children, accent_colour=visual.colour('tournament')))
    # 삭제된 대회는 순위표 주소가 열리지 않음
    if state != 'gone':
        view.add_item(ui.ActionRow(link_button(b)))
    return view


def csv_text(b: dict) -> str:
    """/대회로 연결한 사람에게만 보내는 CSV 안내"""
    if b['groupCount'] > 1:
        return f"이 채널에 올린 결과 CSV는 {b['group']}조 순위에 반영됩니다."
    return "이 채널에 올린 결과 CSV는 순위에 반영됩니다."


def info_view(text: str) -> ui.LayoutView:
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(ui.TextDisplay(text), accent_colour=visual.colour('tournament')))
    return view


def replays_of(b: dict) -> list:
    rows = b.get('replays')
    return [r for r in rows if isinstance(r, dict) and r.get('gameId') and isinstance(r.get('roundNo'), int)] if isinstance(rows, list) else []


def replay_lines(b: dict) -> list:
    """라운드별 게임 ID를 한 줄에 두 개씩"""
    codes = [f"{r['roundNo']}R `{r['gameId']}`" for r in replays_of(b)]
    pairs = ["   ".join(codes[i:i + 2]) for i in range(0, len(codes), 2)]
    return ["-# 리플레이 코드", *pairs] if codes else []


def already_text(b: dict) -> str:
    name = discord.utils.escape_markdown(b['name'])
    if b['groupCount'] > 1:
        return f"이 채널은 이미 {name} {b['group']}조에 연결돼 있습니다."
    return f"이 채널은 이미 {name}에 연결돼 있습니다."


def round_view(b: dict, png: bytes) -> ui.LayoutView:
    """자동 갱신이 채널에 올리는 라운드 순위 이미지. 끝난 대회는 최종 순위 아래에 라운드별 게임 ID를 둠"""
    view = ui.LayoutView(timeout=None)
    children = [gallery(view, png)]
    lines = replay_lines(b) if b['ended'] else next_lines(b['next']) if b.get('next') else []
    if lines:
        children.append(ui.TextDisplay('\n'.join(lines)))
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


def result_view(r: dict) -> ui.LayoutView:
    """relay 응답 added, duplicate, ended"""
    if r['status'] == 'added':
        title = r['name'] + (f" {r['group']}조" if r['groupCount'] > 1 else '') + f" {r['round']}라운드 게임 순위"
        lines = [f"### {discord.utils.escape_markdown(title)}"]
        lines += [f"{t['rank']}위 {discord.utils.escape_markdown(t['team'])} {t['score']}점" for t in r['ranking']]
        text = '\n'.join(lines)
    else:
        text = discord.utils.escape_markdown(r['message'])
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(ui.TextDisplay(text), accent_colour=visual.colour('tournament')))
    view.add_item(ui.ActionRow(ui.Button(style=discord.ButtonStyle.link, label="순위표", url=r['url'])))
    return view


# 자동 갱신 기록. message_id는 안내 메시지, image_id는 마지막으로 올린 순위 이미지 메시지.
# started_by는 /대회로 켠 사람이고, Labs 관리 화면 연결로 켠 보드는 0

def live_get(message_id: int) -> Optional[sqlite3.Row]:
    return db.execute("SELECT * FROM live_boards WHERE message_id = ?", (message_id,)).fetchone()


def live_put(message_id: int, channel_id: int, guild_id: int, b: dict, user_id: int, notice: str) -> None:
    # version 0이면 다음 tick이 since 없이 조회해 첫 이미지를 못 올렸어도 다시 올림
    db.execute("INSERT OR REPLACE INTO live_boards (message_id, channel_id, guild_id, code, grp, version, "
               "started_by, started_at, board, image_id, last_round, notice) "
               "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, NULL, 0, ?)",
               (message_id, channel_id, guild_id, b['code'], b['group'], user_id, int(time.time()),
                json.dumps(b, ensure_ascii=False), notice))


def live_save(message_id: int, b: dict, image_id: Optional[int], last_round: int) -> None:
    db.execute("UPDATE live_boards SET version = ?, board = ?, image_id = ?, last_round = ? WHERE message_id = ?",
               (b['version'], json.dumps(b, ensure_ascii=False), image_id, last_round, message_id))


def live_note(message_id: int, notice: str) -> None:
    db.execute("UPDATE live_boards SET notice = ? WHERE message_id = ?", (notice, message_id))


def live_drop(message_id: int) -> None:
    db.execute("DELETE FROM live_boards WHERE message_id = ?", (message_id,))


def live_running(channel_id: int, code: str, group: str) -> bool:
    return db.execute("SELECT 1 FROM live_boards WHERE channel_id = ? AND code = ? AND grp = ?",
                      (channel_id, code, group)).fetchone() is not None


def boards_in(channel_id: int) -> List[sqlite3.Row]:
    return db.execute("SELECT * FROM live_boards WHERE channel_id = ?", (channel_id,)).fetchall()


def boards_of(code: str, group: Optional[str] = None) -> List[sqlite3.Row]:
    if group is None:
        return db.execute("SELECT * FROM live_boards WHERE code = ?", (code,)).fetchall()
    return db.execute("SELECT * FROM live_boards WHERE code = ? AND grp = ?", (code, group)).fetchall()


def is_operator(b: dict) -> bool:
    return b.get('role') in ('owner', 'operator')


async def reply_error(interaction: discord.Interaction, text: str) -> None:
    view = create_error_layout(text)
    if interaction.response.is_done():
        await interaction.followup.send(view=view, ephemeral=True)
    else:
        await interaction.response.send_message(view=view, ephemeral=True)


async def reply_private(interaction: discord.Interaction, text: str) -> None:
    """본인에게만 보이는 안내. 로딩 카드를 이미 채널에 올렸으면 지우고 보냄"""
    if not interaction.response.is_done():
        await interaction.response.send_message(view=info_view(text), ephemeral=True)
        return
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass
    await interaction.followup.send(view=info_view(text), ephemeral=True)


_last_refresh: Dict[object, float] = {}


def too_soon(key) -> bool:
    """새로고침과 조 선택은 메시지마다와 사람마다 REFRESH_GAP 간격"""
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


def live_limit(guild_id: int, user_id: int, ignore: Tuple[int, ...] = ()) -> Optional[str]:
    """자동 갱신 개수 제한. user_id 0은 관리 화면 연결이라 사람당 한도를 보지 않음. ignore는 곧 멈출 보드"""
    rows = [r for r in db.execute("SELECT message_id, guild_id, started_by FROM live_boards")
            if r['message_id'] not in ignore]
    if sum(r['guild_id'] == guild_id for r in rows) >= LIVE_PER_GUILD:
        return (f"이 서버에서는 자동 갱신을 {LIVE_PER_GUILD}개까지 켤 수 있습니다.\n"
                "Labs 관리 화면에서 다른 채널의 연결을 해제한 뒤 다시 시도해주세요.")
    if user_id and sum(r['started_by'] == user_id for r in rows) >= LIVE_PER_USER:
        return (f"자동 갱신은 한 사람이 {LIVE_PER_USER}개까지 켤 수 있습니다.\n"
                "Labs 관리 화면에서 다른 채널의 연결을 해제한 뒤 다시 시도해주세요.")
    if len(rows) >= LIVE_TOTAL:
        return "지금은 자동 갱신을 더 켤 수 없습니다.\n잠시 후 다시 시도해주세요."
    return None


def missing_perms(perms: discord.Permissions, thread: bool = False) -> List[str]:
    """자동 갱신과 결과 답장에 필요한 봇 채널 권한 가운데 빠진 것"""
    send = perms.send_messages_in_threads if thread else perms.send_messages
    return [name for name, ok in (('채널 보기', perms.view_channel), ('메시지 보내기', send),
                                  ('파일 첨부', perms.attach_files),
                                  ('메시지 기록 보기', perms.read_message_history)) if not ok]


def live_blocked(interaction: discord.Interaction, ignore: Tuple[int, ...] = ()) -> Optional[str]:
    """채널을 연결할 수 없는 이유. 봇 채널 권한과 개수 제한"""
    missing = missing_perms(interaction.app_permissions, isinstance(interaction.channel, discord.Thread))
    if missing:
        names = ', '.join(missing)
        return (f"몽실봇에 이 채널의 {names} 권한이 없어 자동 갱신을 켤 수 없습니다.\n"
                f"채널 권한에서 몽실봇에 {names}를 허용해주세요.")
    return live_limit(interaction.guild.id, interaction.user.id, ignore)


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
        super().__init__(ui.Button(label="새로고침", style=discord.ButtonStyle.secondary,
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
        await reply_error(interaction, "자동 갱신을 켜려면 /대회를 다시 입력해주세요.")


class LiveOffButton(ui.DynamicItem[ui.Button], template=r'tx:(?P<code>[0-9A-Z]{6,7}):(?P<g>[A-H])'):
    """배포 전 안내 메시지에 남은 자동 갱신 끄기 버튼. 아무것도 바꾸지 않음"""
    def __init__(self, code: str, group: str):
        super().__init__(ui.Button(label="자동 갱신 끄기", style=discord.ButtonStyle.secondary,
                                   custom_id=f"tx:{code}:{group}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match['code'], match['g'])

    async def callback(self, interaction: discord.Interaction):
        await reply_error(interaction, OLD_OFF_BUTTON)


class RelayLinkButton(ui.DynamicItem[ui.Button], template=r'tl:(?P<code>[0-9A-Z]{6,7})'):
    """배포 전 안내 메시지에 남은 결과 받기 버튼. 아무것도 바꾸지 않음"""
    def __init__(self, code: str):
        super().__init__(ui.Button(label="결과 받기", style=discord.ButtonStyle.secondary, custom_id=f"tl:{code}"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match['code'])

    async def callback(self, interaction: discord.Interaction):
        await reply_error(interaction, OLD_RELAY_BUTTON)


# 연결 사본. linked는 연결된 채널 ID로, 연결 안 된 채널의 메시지는 DB도 보지 않고 넘김.
# /대회 연결, changed 알림, 60초 동기화는 links_lock 안에서 Labs 조회부터 사본 반영까지 한 번에 함

Link = Tuple[str, str]


def load_linked() -> set:
    return {r[0] for r in db.execute("SELECT channel_id FROM result_links")}


linked = load_linked()
links_lock = asyncio.Lock()
# /대회가 안내 메시지를 올리는 중인 (채널, 대회, 조). changed 처리가 같은 안내를 또 올리지 않게 함
_starting: set = set()
# 관리 화면 연결 중 일시 오류로 안내를 못 올린 채널. 다음 전체 동기화에서 다시 시도
_retry: Dict[int, Link] = {}


def copy_rows(code: Optional[str] = None) -> Dict[int, Link]:
    if code is None:
        rows = db.execute("SELECT channel_id, code, grp FROM result_links")
    else:
        rows = db.execute("SELECT channel_id, code, grp FROM result_links WHERE code = ?", (code,))
    return {r['channel_id']: (r['code'], r['grp']) for r in rows}


def copy_guild(channel_id: int) -> Optional[int]:
    row = db.execute("SELECT guild_id FROM result_links WHERE channel_id = ?", (channel_id,)).fetchone()
    return row['guild_id'] if row else None


def copy_set(channel_id: int, guild_id: int, code: str, group: str) -> None:
    db.execute("INSERT OR REPLACE INTO result_links (channel_id, guild_id, code, grp, saved_at) VALUES (?, ?, ?, ?, ?)",
               (channel_id, guild_id, code, group, int(time.time())))
    linked.add(channel_id)


def copy_drop(channel_id: int) -> None:
    db.execute("DELETE FROM result_links WHERE channel_id = ?", (channel_id,))
    linked.discard(channel_id)


def links_map(links: list, code: Optional[str] = None) -> Dict[int, Link]:
    """Labs 연결 목록을 채널 ID -> (대회, 조)로. code를 주면 대회별 목록"""
    out = {}
    for link in links:
        cid = link.get('channelId')
        if cid and str(cid).isdigit():
            out[int(cid)] = (code or link['code'], link['group'])
    return out


def diff_links(old: Dict[int, Link], new: Dict[int, Link]) -> Tuple[Dict[int, Link], Dict[int, Link]]:
    """새로 생긴 연결과 사라진 연결. 채널의 대회나 조가 바뀌면 양쪽에 다 들어감"""
    added = {c: v for c, v in new.items() if old.get(c) != v}
    removed = {c: v for c, v in old.items() if new.get(c) != v}
    return added, removed


def stop_state(channel_id: int, link: Link, links: Dict[int, Link]) -> str:
    """사라진 연결의 보드를 멈춘 이유. links는 지금의 연결 전체"""
    now = links.get(channel_id)
    if now and now != link:
        return 'replaced'
    if any(v == link for c, v in links.items() if c != channel_id):
        return 'moved'
    return 'unlinked'


_kept: Dict[int, Tuple[float, int, int, int, str, str, bytes]] = {}
# 다시 보내기로 모두 반영되면 지울 원래 메시지 ID -> 아직 반영되지 않은 첨부 ID
_pending: Dict[int, set] = {}
# 다시 보내는 중이라 _kept에서 꺼낸 첨부 ID
_relaying: set = set()


def keep_file(key: int, channel_id: int, message_id: int, author_id: int, author_name: str, file_name: str,
              data: bytes) -> None:
    """다시 보내기용 파일 바이트. RELAY_KEEP_SECONDS 동안 메모리에만"""
    now = time.monotonic()
    for k in [k for k, v in _kept.items() if v[0] < now]:
        del _kept[k]
    while len(_kept) >= RELAY_KEEP_MAX:
        del _kept[next(iter(_kept))]
    # 보관이 끝난 첨부가 있는 메시지는 다시 모두 반영될 수 없음
    for mid in [m for m, keys in _pending.items() if not keys <= _kept.keys() | _relaying]:
        del _pending[mid]
    _kept[key] = (now + RELAY_KEEP_SECONDS, channel_id, message_id, author_id, author_name, file_name, data)


def retry_view(key: int) -> ui.LayoutView:
    view = create_error_layout(RELAY_DOWN)
    view.add_item(ui.ActionRow(RelayRetryButton(key)))
    return view


def log_result(r: dict) -> None:
    logger.info(f"결과 전달 {r.get('code') or '-'} {r.get('group') or '-'} {r['status']}")


async def delete_quietly(message) -> None:
    """이미 지워졌거나 권한이 없으면 넘어감"""
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden):
        pass
    except discord.HTTPException as e:
        logger.warning(f"결과 메시지 지우기 실패: {type(e).__name__}: {e}")


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
            return await reply_error(interaction, KEEP_EXPIRED)
        if too_soon(('relay', interaction.user.id)):
            return await reply_error(interaction, TOO_SOON)
        await interaction.response.defer()
        # 동시에 눌러도 한 번만 보내도록 먼저 꺼내고, 실패하면 되돌림. 이름은 누른 사람이 아닌 올린 사람
        _kept.pop(self.key, None)
        _relaying.add(self.key)
        _, channel_id, message_id, author_id, author_name, file_name, data = kept
        cog = interaction.client.get_cog('Tournament')
        before = cog.posted_before(channel_id) if cog else None
        try:
            r = await relay(channel_id, author_id, author_name, file_name, data)
        finally:
            _relaying.discard(self.key)
        if r is None:
            _kept[self.key] = kept
            return await reply_error(interaction, RELAY_DOWN)
        log_result(r)
        status = r['status']
        if status == 'unlinked':
            _pending.pop(message_id, None)
            if cog:
                await cog.relay_unlinked(channel_id)
            await interaction.message.delete()
        elif status == 'invalid':
            _pending.pop(message_id, None)
            await interaction.edit_original_response(view=create_error_layout(NOT_CSV))
        elif status == 'added':
            # 이미지를 올리는 사이 keep_file이 불려도 남도록 먼저 뺌
            _pending.get(message_id, set()).discard(self.key)
            channel = interaction.client.get_partial_messageable(channel_id)
            shown = bool(cog) and await cog.show_board(channel, r, before)
            if not shown:
                _pending.pop(message_id, None)
                return await interaction.edit_original_response(view=result_view(r))
            await delete_quietly(interaction.message)
            if _pending.get(message_id) == set():
                del _pending[message_id]
                if interaction.app_permissions.manage_messages:
                    await delete_quietly(channel.get_partial_message(message_id))
        else:
            _pending.pop(message_id, None)
            await interaction.edit_original_response(view=result_view(r))


# 채널 목록. Labs 관리 화면이 조마다 채널을 고를 때 씀

CHANNELS_TTL = 60
_channels: Dict[int, Tuple[float, dict]] = {}


def channel_allowed(user: discord.Permissions, bot: discord.Permissions) -> bool:
    return (user.view_channel and user.send_messages and bot.view_channel and bot.send_messages
            and bot.read_message_history and bot.attach_files)


def candidate_guilds(user_id: int) -> set:
    """사용자가 명령을 쓴 서버, 켠 자동 갱신이 있는 서버, 연결이 남아 있는 서버"""
    ids = {r[0] for r in db.execute("SELECT DISTINCT guild_id FROM command_log WHERE user_id = ? "
                                    "AND guild_id IS NOT NULL", (user_id,))}
    ids |= {r[0] for r in db.execute("SELECT DISTINCT guild_id FROM live_boards WHERE started_by = ?", (user_id,))}
    ids |= {r[0] for r in db.execute("SELECT DISTINCT guild_id FROM result_links")}
    return ids


def guild_channels(guild: discord.Guild, member: discord.Member) -> List[dict]:
    """텍스트와 공지 채널을 디스코드에 보이는 순서로"""
    me = guild.me
    return [{'id': str(c.id), 'name': c.name}
            for _, channels in guild.by_category() for c in channels
            if isinstance(c, discord.TextChannel) and channel_allowed(c.permissions_for(member), c.permissions_for(me))]


async def channel_list(client: discord.Client, user_id: int) -> dict:
    hit = _channels.get(user_id)
    if hit and time.monotonic() - hit[0] < CHANNELS_TTL:
        return hit[1]
    guilds = [g for g in (client.get_guild(gid) for gid in candidate_guilds(user_id)) if g is not None]

    async def one(guild: discord.Guild) -> Optional[dict]:
        try:
            member = await guild.fetch_member(user_id)
        except (discord.HTTPException, asyncio.TimeoutError):
            return None
        channels = guild_channels(guild, member)
        return {'id': str(guild.id), 'name': guild.name, 'channels': channels} if channels else None

    found = [g for g in await asyncio.gather(*(one(g) for g in guilds)) if g]
    result = {'guilds': sorted(found, key=lambda g: g['name'].casefold())}
    if len(_channels) > 1000:
        _channels.clear()
    _channels[user_id] = (time.monotonic(), result)
    return result


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


class AlreadyLinked(Exception):
    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


class Tournament(commands.Cog):
    def __init__(self, client: ERClient):
        self.client = client
        # 보드마다 갱신은 한 번에 하나. 값이 True면 갱신 중에 알림이 또 와서 끝난 뒤 한 번 더 돎
        self._refreshing: Dict[int, bool] = {}
        self._idle: Dict[int, asyncio.Event] = {}
        # (채널, 대회, 조)마다 올리거나 고친 순위 이미지 수. 마지막 라운드로 보드 행이 지워져도 남음
        self._posted: Dict[Tuple[int, str, str], int] = {}
        self._info_failed: Dict[int, float] = {}

    async def cog_load(self):
        self.client.add_dynamic_items(RefreshButton, GroupSelect, LiveButton, LiveOffButton, RelayLinkButton,
                                      RelayRetryButton)
        self.live.start()
        self.links_loop.start()

    async def cog_unload(self):
        self.live.cancel()
        self.links_loop.cancel()

    # 보드 갱신

    @tasks.loop(seconds=LIVE_SECONDS)
    async def live(self):
        try:
            rows = db.execute("SELECT message_id FROM live_boards").fetchall()
        except sqlite3.Error as e:
            logger.warning(f"자동 갱신 목록 조회 실패: {e}")
            return
        limit = asyncio.Semaphore(5)

        async def one(mid: int):
            async with limit:
                await self.refresh(mid)

        await asyncio.gather(*(one(r['message_id']) for r in rows))

    @live.before_loop
    async def _before_live(self):
        await self.client.wait_until_ready()

    async def refresh(self, mid: int, wait: bool = False) -> None:
        """wait면 이미 갱신 중일 때 그 갱신이 한 번 더 돌고 끝날 때까지 기다림"""
        if mid in self._refreshing:
            self._refreshing[mid] = True
            if wait:
                await self._idle[mid].wait()
            return
        self._refreshing[mid] = False
        self._idle[mid] = idle = asyncio.Event()
        try:
            while True:
                row = live_get(mid)
                if row is None:
                    return
                await self.tick(row)
                if not self._refreshing[mid]:
                    return
                self._refreshing[mid] = False
        finally:
            del self._refreshing[mid]
            del self._idle[mid]
            idle.set()

    async def refresh_code(self, code: str) -> None:
        await asyncio.gather(*(self.refresh(r['message_id']) for r in boards_of(code)))

    def posted_before(self, channel_id: int) -> Tuple[Optional[Link], int]:
        """CSV를 Labs로 보내기 전 그 채널 조의 이미지 수. 보내는 사이 다른 갱신이 올린 이미지도 셈"""
        link = copy_rows().get(channel_id)
        return link, self._posted.get((channel_id, *link), 0) if link else 0

    async def show_board(self, channel, r: dict, before: Tuple[Optional[Link], int]) -> bool:
        """CSV를 반영한 조의 순위 이미지를 올림. 올린 것을 확인하면 True"""
        code, group = r.get('code'), r.get('group')
        if not code or not group:
            return False
        key = (channel.id, code, group)
        count = before[1] if before[0] == (code, group) else self._posted.get(key, 0)
        rows = [row for row in boards_of(code, group) if row['channel_id'] == channel.id]
        if rows:
            await self.refresh(rows[0]['message_id'], wait=True)
            return self._posted.get(key, 0) > count
        # 마지막 라운드면 다른 갱신이 마지막 이미지를 올리고 보드 행을 이미 지웠을 수 있음
        if self._posted.get(key, 0) > count:
            return True
        try:
            b = await fetch_board(code, group)
            view = round_view(b, await fetch_image(code, group))
            await channel.send(view=view, files=visual.files_of(view))
        except (NotFoundError, ServiceDownError, ValidationError) as e:
            logger.warning(f"결과 순위 이미지 조회 실패 {code}: {e.message}")
            return False
        except discord.HTTPException as e:
            logger.warning(f"결과 순위 이미지 실패 {code}: {type(e).__name__}: {e}")
            return False
        return True

    async def tick(self, row: sqlite3.Row):
        mid = row['message_id']
        channel = self.client.get_partial_messageable(row['channel_id'])
        notice = channel.get_partial_message(mid)
        image_id, last_round = row['image_id'], row['last_round']
        try:
            try:
                b = await fetch_board(row['code'], row['grp'], since=row['version'])
                png = await fetch_image(row['code'], row['grp']) if b is not UNCHANGED and b['played'] else None
            except (NotFoundError, ValidationError) as e:
                if live_get(mid):
                    await self.stop(row, 'gone' if isinstance(e, NotFoundError) else 'nogroup')
                return
            except ServiceDownError as e:
                logger.warning(f"자동 갱신 조회 실패 {row['code']}: {e.message}")
                return
            # 조회하는 사이 보드를 멈췄으면 아무것도 올리지 않음
            if not live_get(mid):
                return
            if b is UNCHANGED:
                # 배포 전 안내나 이미지를 못 받은 안내는 바뀐 것이 없어도 다시 고침
                old = json.loads(row['board'])
                if notice_key(old) != row['notice'] and not self.info_waiting(mid):
                    # 배포 전 기록에는 개최자가 없어 한 번 새로 받음
                    if 'host' not in old:
                        try:
                            full = await fetch_board(row['code'], row['grp'])
                            old = old if full is UNCHANGED else full
                        except (NotFoundError, ServiceDownError, ValidationError):
                            self._info_failed[mid] = time.time()
                            return
                    await self.fix_notice(row, notice, old)
                return
            # 라운드를 모두 지웠으면 빈 순위표 대신 마지막 이미지를 지움
            if not b['played'] and image_id:
                # 지우지 못하면 기록을 남기지 않아 다음 tick에 다시 지움
                try:
                    await channel.get_partial_message(image_id).delete()
                except discord.NotFound:
                    pass
                image_id, last_round = None, 0
                if not live_get(mid):
                    return
            if png is not None:
                new_round = b['played'] > last_round
                image_id = await post_round(channel, b, png, None if new_round else image_id)
                last_round = b['played']
                key = (row['channel_id'], row['code'], row['grp'])
                self._posted[key] = self._posted.get(key, 0) + 1
                # 이미지를 올리는 사이 보드를 멈췄으면 안내 메시지를 고치지 않음
                if not live_get(mid):
                    return
            if b['ended']:
                live_drop(mid)
                await notice.edit(view=notice_view(b, 'ended', image=has_image(row['notice'])))
                return
            live_save(mid, b, image_id, last_round)
            # 안내 내용이 같으면 고치지 않음. 봇이 꺼진 사이 지워졌으면 NotFound로 끔
            if notice_key(b) != row['notice']:
                await self.fix_notice(row, notice, b)
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

    def info_waiting(self, mid: int) -> bool:
        return time.time() - self._info_failed.get(mid, 0) < INFO_RETRY_SECONDS

    async def fix_notice(self, row: sqlite3.Row, notice, b: dict) -> None:
        """안내 메시지를 대회 정보 이미지와 함께 다시 씀.
        이미지를 못 받으면 글만 고치고 옛 이미지 표시로 남겨 뒤의 tick이 다시 시도"""
        mid = row['message_id']
        info = None
        if not self.info_waiting(mid):
            try:
                info = await fetch_info_image(b, self.client.get_guild(row['guild_id']))
                self._info_failed.pop(mid, None)
            except (NotFoundError, ServiceDownError, ValidationError) as e:
                self._info_failed[mid] = time.time()
                logger.warning(f"자동 갱신 안내 이미지 조회 실패 {row['code']}: {type(e).__name__}")
        if not live_get(mid):
            return
        if info is not None:
            view = notice_view(b, png=info)
            await notice.edit(view=view, attachments=visual.files_of(view))
            live_note(mid, notice_key(b))
            return
        mark = IMAGE if has_image(row['notice']) else ''
        if notice_key(b, mark) != row['notice']:
            await notice.edit(view=notice_view(b, image=bool(mark)))
            live_note(mid, notice_key(b, mark))

    async def stop(self, row: sqlite3.Row, state: str) -> None:
        """보드를 멈추고 안내 메시지를 멈춘 이유로 고침"""
        live_drop(row['message_id'])
        notice = self.client.get_partial_messageable(row['channel_id']).get_partial_message(row['message_id'])
        try:
            await notice.edit(view=notice_view(json.loads(row['board']), state, image=has_image(row['notice'])))
        except discord.HTTPException as e:
            logger.info(f"자동 갱신 안내 고치기 실패 {row['code']} {state}: {type(e).__name__}")
        logger.info(f"자동 갱신 멈춤 {row['code']} {row['grp']} {state}")

    async def start(self, channel, guild_id: int, b: dict, png: Optional[bytes], user_id: int,
                    message_id: Optional[int] = None, image: bool = False) -> None:
        """안내 메시지와 첫 이미지를 올리고 자동 갱신을 켬. message_id는 /대회가 이미 올린 안내 메시지,
        image는 그 메시지에 대회 정보 이미지가 있는지. 이미지를 못 받으면 글로 올리고 tick이 나중에 고침"""
        if message_id is None:
            try:
                info = await fetch_info_image(b, getattr(channel, 'guild', None))
            except (NotFoundError, ServiceDownError, ValidationError):
                info = None
            view = notice_view(b, png=info)
            message_id = (await channel.send(view=view, files=visual.files_of(view))).id
            image = info is not None
        # 첫 이미지를 올리는 사이 보드가 멈출 수 있어 기록부터 남김
        live_put(message_id, channel.id, guild_id, b, user_id, notice_key(b, INFO if image else ''))
        if png:
            try:
                image_id = await post_round(channel, b, png, None)
            except discord.HTTPException as e:
                logger.warning(f"자동 갱신 첫 이미지 실패 {b['code']}: {type(e).__name__}: {e}")
            else:
                if live_get(message_id):
                    live_save(message_id, b, image_id, b['played'])
        logger.info(f"자동 갱신 켬 {b['code']} {b['group']} 서버 {guild_id}")

    async def start_linked(self, channel_id: int, link: Link) -> None:
        """관리 화면에서 고른 연결의 안내 메시지. 권한이 없거나 한도에 걸리면 연결만 둠"""
        _retry.pop(channel_id, None)
        code, group = link
        channel = self.client.get_channel(channel_id)
        guild = getattr(channel, 'guild', None)
        if guild is None:
            return
        missing = missing_perms(channel.permissions_for(guild.me), isinstance(channel, discord.Thread))
        ignore = tuple(r['message_id'] for r in boards_in(channel_id))
        if missing or live_limit(guild.id, 0, ignore):
            logger.info(f"연결 안내 생략 {code} {group}: {', '.join(missing) or '한도'}")
            return
        try:
            b = await fetch_board(code, group)
            if not b['ended']:
                await self.start(channel, guild.id, b, await fetch_image(code, group) if b['played'] else None, 0)
        except (NotFoundError, ServiceDownError, ValidationError) as e:
            logger.info(f"연결 안내 실패 {code} {group}: {e.message}")
            if isinstance(e, ServiceDownError):
                _retry[channel_id] = link
        except discord.HTTPException as e:
            logger.warning(f"연결 안내 메시지 실패 {code}: {type(e).__name__}: {e}")
            if e.status >= 500:
                _retry[channel_id] = link

    # 연결 사본 맞추기. 모두 links_lock 안에서 부름

    def guild_of(self, channel_id: int) -> int:
        guild = getattr(self.client.get_channel(channel_id), 'guild', None)
        return guild.id if guild else copy_guild(channel_id) or 0

    async def apply(self, code: Optional[str], new: Dict[int, Link], post: bool, skip: Optional[int] = None) -> None:
        """사본을 Labs 목록에 맞춤. code가 있으면 그 대회만. post면 새 연결에 안내 메시지를 올림"""
        old = copy_rows(code)
        current = new if code is None else {**{c: v for c, v in copy_rows().items() if v[0] != code}, **new}
        added, removed = diff_links(old, new)
        for cid, link in removed.items():
            if cid not in added:
                copy_drop(cid)
            for row in boards_of(*link):
                if row['channel_id'] != cid:
                    continue
                # 전체 목록에는 끝난 대회가 없어서 먼저 갱신해 끝남과 삭제를 가림
                if code is None:
                    await self.refresh(row['message_id'])
                    if not live_get(row['message_id']):
                        continue
                await self.stop(row, stop_state(cid, link, current))
        for cid, link in added.items():
            copy_set(cid, self.guild_of(cid), *link)
            for row in boards_in(cid):
                if (row['code'], row['grp']) != link:
                    await self.stop(row, 'replaced')
            if cid == skip or live_running(cid, *link) or (cid, *link) in _starting:
                continue
            if post:
                await self.start_linked(cid, link)
            else:
                # 60초 동기화가 changed 알림보다 먼저 받은 연결은 안내를 미뤄 둠
                _retry[cid] = link
        if post:
            for cid, link in list(_retry.items()):
                if cid not in added and cid != skip and new.get(cid) == link and not live_running(cid, *link) and (cid, *link) not in _starting:
                    await self.start_linked(cid, link)

    async def sync_code(self, code: str) -> None:
        async with links_lock:
            try:
                links = await fetch_links(code)
            except NotFoundError:
                for cid in copy_rows(code):
                    copy_drop(cid)
                for row in boards_of(code):
                    await self.stop(row, 'gone')
                return
            except (ServiceDownError, ValidationError) as e:
                logger.warning(f"연결 조회 실패 {code}: {e.message}")
                return
            await self.apply(code, links_map(links, code), post=True)

    async def sync_all(self) -> None:
        async with links_lock:
            try:
                links = await fetch_all_links()
            except (NotFoundError, ServiceDownError, ValidationError) as e:
                logger.warning(f"연결 전체 조회 실패: {e.message}")
                return
            try:
                new = links_map(links)
            except (KeyError, TypeError):
                logger.warning("연결 전체 응답 형식 오류")
                return
            # 전체 목록에는 끝난 대회가 없어 사본에만 남은 대회는 대회별 목록으로 확인
            for code in {v[0] for v in copy_rows().values()} - {v[0] for v in new.values()}:
                try:
                    for cid, link in links_map(await fetch_links(code), code).items():
                        new.setdefault(cid, link)
                except NotFoundError:
                    continue
                except (ServiceDownError, ValidationError) as e:
                    logger.warning(f"연결 조회 실패 {code}: {e.message}")
                    return
            pending = dict(_retry)
            await self.apply(None, new, post=False)
            for cid, link in pending.items():
                if new.get(cid) == link and not live_running(cid, *link) and (cid, *link) not in _starting:
                    await self.start_linked(cid, link)
                elif _retry.get(cid) == link:
                    _retry.pop(cid)

    async def on_changed(self, code: str) -> None:
        """Labs 변경 알림. 연결을 맞추고 그 대회의 보드를 바로 갱신"""
        try:
            await self.sync_code(code)
            await self.refresh_code(code)
        except Exception:
            logger.error(f"변경 알림 처리 실패 {code}", exc_info=True)

    async def relay_unlinked(self, channel_id: int) -> None:
        """Labs가 연결 없음으로 답한 채널. Labs 목록으로 사본을 다시 맞춤"""
        link = copy_rows().get(channel_id)
        if link:
            await self.sync_code(link[0])

    @tasks.loop(seconds=LINKS_SECONDS)
    async def links_loop(self):
        try:
            await self.sync_all()
        except Exception:
            logger.error("연결 동기화 실패", exc_info=True)

    @links_loop.before_loop
    async def _before_links(self):
        await self.client.wait_until_ready()

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        # 안내 메시지를 지우면 그 보드만 멈추고 연결은 남김
        live_drop(payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        for mid in payload.message_ids:
            live_drop(mid)

    # 결과 CSV 받기

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
                    await message.reply(view=create_error_layout(DOWNLOAD_FAILED), mention_author=False)
                except discord.HTTPException:
                    pass
        before = self.posted_before(message.channel.id)
        added, kept = [], set()
        # 본문이나 스티커가 있으면 CSV를 모두 반영해도 메시지를 남김
        complete = len(loaded) == len(message.attachments) and not message.content and not message.stickers
        for a, data in loaded:
            r = None
            try:
                r = await self.relay_one(message, a, data)
            except discord.HTTPException as e:
                logger.warning(f"결과 전달 답장 실패: {type(e).__name__}: {e}")
            except Exception:
                logger.error("결과 전달 처리 실패", exc_info=True)
            if r is not None and r['status'] == 'added':
                added.append(r)
            elif a.id in _kept:
                kept.add(a.id)
            else:
                complete = False
            if message.channel.id not in linked:
                return
        # 다른 첨부를 보내는 사이 다시 보내기가 끝난 파일은 결과를 알 수 없어 메시지를 남김
        if kept - (_kept.keys() | _relaying):
            complete = False
        # 이미지를 올리는 사이 다시 보내기가 끝나도 지울 수 있도록 먼저 등록
        if complete and kept:
            _pending[message.id] = kept
        if added and not await self.show_board(message.channel, added[-1], before):
            _pending.pop(message.id, None)
            for r in added:
                try:
                    await message.reply(view=result_view(r), mention_author=False)
                except discord.HTTPException as e:
                    logger.warning(f"결과 전달 답장 실패: {type(e).__name__}: {e}")
            return
        if complete and not kept and added and message.channel.permissions_for(message.guild.me).manage_messages:
            await delete_quietly(message)

    async def relay_one(self, message: discord.Message, a: discord.Attachment,
                        data: Optional[bytes]) -> Optional[dict]:
        """첨부 하나를 Labs로 보내고 relay 응답을 돌려줌. 반영되지 않았으면 이유를 답장"""
        def reply(view, **kwargs):
            return message.reply(view=view, mention_author=False, **kwargs)

        if data is None:
            await reply(create_error_layout(NOT_CSV))
            return None
        name = message.author.display_name[:ACTOR_NAME_MAX]
        r = await relay(message.channel.id, message.author.id, name, a.filename, data)
        if r is None:
            keep_file(a.id, message.channel.id, message.id, message.author.id, name, a.filename, data)
            await reply(retry_view(a.id))
            return None
        log_result(r)
        status = r['status']
        if status == 'unlinked':
            await self.relay_unlinked(message.channel.id)
        elif status == 'invalid':
            await reply(create_error_layout(NOT_CSV))
        elif status != 'added':
            await reply(result_view(r))
        return r

    # /대회

    async def code_autocomplete(self, interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
        rows = await my_tournaments(interaction.user.id)
        q = (current or '').strip().lower()
        choices = []
        for t in rows:
            if q and q not in t['name'].lower() and q not in t['code'].lower():
                continue
            state = f"{t['played']}라운드 진행" if t['played'] else "경기 전"
            role = "개최자" if t['role'] == 'owner' else "운영자"
            name = f"{t['name']} | {t['code']} | {role} | {state}"
            choices.append(app_commands.Choice(name=name[:100], value=t['code']))
        return choices[:25]

    async def link_here(self, interaction: discord.Interaction, b: dict, live: dict) -> Optional[str]:
        """/대회를 입력한 채널을 그 조에 연결하고, 막히면 이유를 돌려줌. 안내 메시지는 부른 쪽이 올리고 live['key']를 _starting에서 뺌"""
        channel, guild = interaction.channel, interaction.guild
        code, group = b['code'], b['group']
        if live_running(channel.id, code, group) or (channel.id, code, group) in _starting:
            raise AlreadyLinked(already_text(b))
        # 곧 멈출 보드는 한도에서 뺌. 이 채널의 다른 보드와 다른 채널의 같은 조 보드
        ignore = tuple(r['message_id'] for r in boards_in(channel.id)) + \
            tuple(r['message_id'] for r in boards_of(code, group) if r['channel_id'] != channel.id)
        if reason := live_blocked(interaction, ignore):
            return reason
        live['key'] = (channel.id, code, group)
        _starting.add(live['key'])
        async with links_lock:
            # 기다리는 사이 changed 처리가 안내를 올렸을 수 있음
            if live_running(channel.id, code, group):
                raise AlreadyLinked(already_text(b))
            link = {'group': group, 'channelId': str(channel.id), 'guild': guild.name,
                    'channel': getattr(channel, 'name', '')}
            links = await put_links(code, interaction.user.id, [link], group)
            await self.apply(code, links_map(links, code), post=True, skip=channel.id)

    @app_commands.command(name="대회", description="Labs 대회 순위")
    @app_commands.describe(코드="대회 코드나 Labs 대회 주소", 조="여러 조로 나눈 대회의 조")
    @app_commands.choices(조=[app_commands.Choice(name=f"{g}조", value=g) for g in GROUPS])
    # Labs 봇 토큰 한도(분당 1200회)를 모든 서버가 나눠 씀
    @app_commands.checks.cooldown(3, 10)
    @handle_errors(user_message="대회 순위를 가져오지 못했습니다.\n잠시 후 다시 시도해주세요.")
    async def tournament_command(self, interaction: discord.Interaction, 코드: str,
                                 조: Optional[app_commands.Choice[str]] = None):
        target = parse_target(코드)
        if not target:
            raise ValidationError(f"대회 코드 형식 {코드[:40]!r}", BAD_CODE)
        code, group = target
        picked = 조.value if 조 else group
        channel = interaction.channel
        in_guild = interaction.guild is not None and channel is not None and code != 'EXAMPLE'
        live: dict = {}

        async def build():
            b = await fetch_board(code, picked or 'A', interaction.user.id if in_guild else None)
            # 여러 조인 대회에서 조를 고르지 않았으면 연결하지 않고 카드만 보여 줌
            if not (in_guild and is_operator(b) and not b['ended']) or (b['groupCount'] > 1 and not picked):
                return card_view(b, await fetch_image(code, b['group']))
            png = await fetch_image(code, b['group']) if b['played'] else None
            if reason := await self.link_here(interaction, b, live):
                live['blocked'] = reason
                return card_view(b, png or await fetch_image(code, b['group']))
            live['board'], live['png'] = b, png
            # 연결은 이미 끝나 이미지를 못 받아도 글로 안내하고 tick이 나중에 고침
            try:
                live['image'] = await fetch_info_image(b, interaction.guild)
            except (NotFoundError, ServiceDownError, ValidationError):
                live['image'] = None
            return notice_view(b, png=live['image'])

        try:
            try:
                view, message = await send_card(interaction, "대회 순위 조회 중", build)
            except AlreadyLinked as e:
                await reply_private(interaction, e.text)
                return
            if live.get('blocked'):
                await reply_error(interaction, live['blocked'])
            if view is None or 'board' not in live:
                return
            await self.start(channel, interaction.guild.id, live['board'], live['png'], interaction.user.id, message.id,
                             image=live['image'] is not None)
            await interaction.followup.send(view=info_view(csv_text(live['board'])), ephemeral=True)
        finally:
            _starting.discard(live.get('key'))

    tournament_command.autocomplete('코드')(code_autocomplete)


async def setup(client: ERClient):
    await client.add_cog(Tournament(client))
