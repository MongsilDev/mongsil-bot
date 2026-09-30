import asyncio
from datetime import datetime
from typing import Awaitable, Callable, Optional, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo

import discord
from discord import app_commands, ui
from discord.ext import commands

from client import ERClient
from utils import accounts, app_emojis, visual
from utils.emojis import EMOJIS
from utils.config import config
from utils.errors import APIError, InvalidUidError, NotFoundError, handle_errors, validate_nickname
from utils.layouts import create_error_layout, CooldownLayoutView

KST = ZoneInfo('Asia/Seoul')

# 닉네임, uid, 등록한 디스코드 유저 ID
Target = Tuple[str, Optional[str], Optional[int]]
AfterRegister = Callable[[discord.Interaction, str, str, int], Awaitable[None]]

HINT = "등록하면 /랭크, /플탐을 닉네임 없이 쓸 수 있습니다."


async def account_summary(client: ERClient, user_id: int, uid: str, nickname: str):
    """등록 계정의 이번 시즌 요약 텍스트와 티어 아이콘 번호"""
    from commands.season import get_ranked_season
    from utils.character_names import get_character_name
    from utils.rank_helpers import SERVER_NAMES, fetch_user_rank, fetch_user_stats_solo
    from utils.tier_system import TierSystem

    season = await get_ranked_season()
    if not season:
        return "지금은 전적을 불러오지 못했습니다.", None
    season_id, season_name = season

    async def load(u):
        return await asyncio.gather(fetch_user_rank(client, u, season_id), fetch_user_stats_solo(client, u, season_id))

    try:
        user_rank, stats = await with_account(client, user_id, nickname, uid, load)
    except NotFoundError as e:
        if e.message == "유저 통계 없음":
            return f"-# {season_name} 랭크 기록 없음", None
        return "등록한 계정을 조회할 수 없습니다.\n게임에서 닉네임을 바꿨다면 닉네임 변경으로 다시 등록해주세요.", None
    except APIError:
        return "지금은 전적을 불러오지 못했습니다.", None

    if user_rank and user_rank.get('nickname'):
        accounts.rename(user_id, user_rank['nickname'])
    mmr = int(stats.get('mmr', 0))
    server_rank = int(user_rank.get('serverRank', 0)) if user_rank else 0
    tier = TierSystem.get_tier(mmr, server_rank or int(stats.get('rank', 0)))
    if server_rank and server_rank <= 1000:
        place = f"{SERVER_NAMES.get(user_rank.get('serverCode'), '서버')} {server_rank:,}등"
    else:
        rank, size = int(stats.get('rank', 0)), int(stats.get('rankSize', 0))
        place = f"상위 {rank / size * 100:.2f}%" if rank and size else ""
    games = int(stats.get('totalGames', 0))
    wins = int(stats.get('totalWins', 0))
    lines = [f"{tier} **{mmr:,}** RP" + (f" | {place}" if place else "")]
    detail = [season_name, f"{games:,}게임"]
    if games:
        detail += [f"승률 {wins / games * 100:.0f}%", f"평균 {float(stats.get('averageRank', 0.0)):.1f}등"]
    lines.append("-# " + " | ".join(detail))
    most = sorted(stats.get('characterStats') or [], key=lambda c: c.get('totalGames', 0), reverse=True)[:3]
    faces = [app_emojis.character(c.get('characterCode', 0)) or get_character_name(c.get('characterCode', 0)) for c in most]
    if faces:
        lines.append("모스트 " + " ".join(faces))
    return "\n".join(lines), TierSystem.get_tier_icon(tier)


async def account_view(client: ERClient, user_id: int) -> ui.LayoutView:
    view = CooldownLayoutView(timeout=config.view_timeout_interactive)
    account = accounts.get(user_id)
    if not account:
        register = ui.Button(style=discord.ButtonStyle.primary, label="닉네임 등록")
        register.callback = lambda i: i.response.send_modal(RegisterModal(client))
        view.add_item(ui.Container(
            ui.TextDisplay(f"### 내 계정\n등록한 닉네임이 없습니다.\n-# {HINT}"),
            ui.ActionRow(register),
            accent_colour=visual.colour('info'),
        ))
        return view

    uid, nickname, updated = account
    summary, icon = await account_summary(client, user_id, uid, nickname)
    nickname = (accounts.get(user_id) or account)[1]
    day = datetime.fromtimestamp(updated, KST)
    header = ui.TextDisplay(f"### {nickname}\n{summary}")
    top = (ui.Section(header, accessory=ui.Thumbnail(media=f"https://cdn.mongsil.dev/mongsilbot/tier2/{icon}.png"))
           if icon else header)

    change = ui.Button(style=discord.ButtonStyle.secondary, label="닉네임 변경")
    change.callback = lambda i: i.response.send_modal(RegisterModal(client))
    remove = ui.Button(style=discord.ButtonStyle.danger, label="등록 해제")

    async def unregister(interaction: discord.Interaction):
        accounts.delete(interaction.user.id)
        await interaction.response.edit_message(view=await account_view(client, interaction.user.id))

    remove.callback = unregister
    dakgg = ui.Button(style=discord.ButtonStyle.link, label="DAK.GG", emoji=EMOJIS['chart'],
                      url=f"https://dak.gg/er/players/{quote(nickname)}")
    view.add_item(ui.Container(
        top,
        ui.Separator(),
        ui.TextDisplay(f"-# 내 계정 | {day.month}/{day.day} 등록"),
        ui.ActionRow(dakgg, change, remove),
        accent_colour=visual.tier_colour(icon) if icon else visual.colour('info'),
    ))
    return view


def prompt_view(client: ERClient, after: AfterRegister) -> ui.LayoutView:
    view = CooldownLayoutView(timeout=config.view_timeout_interactive)
    register = ui.Button(style=discord.ButtonStyle.primary, label="닉네임 등록")
    register.callback = lambda i: i.response.send_modal(RegisterModal(client, after))
    view.add_item(ui.Container(
        ui.TextDisplay(f"닉네임을 입력하거나 내 닉네임을 등록해주세요.\n-# {HINT}"),
        ui.ActionRow(register),
        accent_colour=visual.colour('info'),
    ))
    return view


class RegisterModal(ui.Modal, title="닉네임 등록"):
    nickname = ui.TextInput(label="이터널 리턴 닉네임", min_length=2, max_length=20)

    def __init__(self, client: ERClient, after: Optional[AfterRegister] = None):
        super().__init__()
        self.client = client
        self.after = after

    @handle_errors(user_message="닉네임을 등록하지 못했습니다. 잠시 후 다시 시도해주세요.")
    async def on_submit(self, interaction: discord.Interaction):
        name = validate_nickname(self.nickname.value)
        await interaction.response.defer()
        uid = await self.client.get_user_nickname(name)
        current = await check_account(self.client, uid) if uid else None
        if current is None:
            await interaction.followup.send(
                view=create_error_layout(f"'{name}' 유저를 찾을 수 없습니다.\n닉네임을 다시 확인해주세요."), ephemeral=True)
            return
        name = current or name
        if not accounts.save(interaction.user.id, uid, name):
            await interaction.followup.send(
                view=create_error_layout("닉네임을 저장하지 못했습니다. 잠시 후 다시 시도해주세요."), ephemeral=True)
            return
        card = await account_view(self.client, interaction.user.id)
        card.message = await interaction.edit_original_response(view=card)
        if self.after:
            await self.after(interaction, name, uid, interaction.user.id)


REREGISTER = "게임에서 닉네임을 바꿨다면 /계정에서 다시 등록해주세요."


async def with_account(client: ERClient, owner: Optional[int], nickname: str, uid: Optional[str], run):
    """등록 계정으로 조회. uid가 무효면 등록 닉네임으로 uid를 다시 찾고, 그래도 안 되면 재등록 안내"""
    try:
        return await run(uid)
    except InvalidUidError:
        if not owner:
            raise
    fresh = await client.get_user_nickname(nickname)
    if fresh and fresh != uid:
        try:
            result = await run(fresh)
        except InvalidUidError:
            pass
        else:
            accounts.save(owner, fresh, nickname)
            return result
    raise NotFoundError(f"등록 계정 조회 실패: {nickname}", f"등록한 '{nickname}' 계정을 조회할 수 없습니다.\n{REREGISTER}")


async def check_account(client: ERClient, uid: str) -> Optional[str]:
    """등록 전 확인. 조회되는 계정이면 최근 게임의 닉네임, 없으면 빈 문자열. 무효 계정이면 None"""
    url = f"{config.api_url}/user/games/uid/{uid}"
    data = await client.api_client.get(url, ttl=300)
    if not data or data.get('message') in ('User Not Found', 'Unauthorized'):
        client.api_client.uncache(url)
        return None
    if data.get('code') not in (200, 404):
        raise APIError(f"계정 확인 실패: {data.get('message')}", "닉네임을 확인하지 못했습니다. 잠시 후 다시 시도해주세요.")
    games = data.get('userGames') or []
    return games[0].get('nickname', '') if games else ''


async def resolve(client: ERClient, interaction: discord.Interaction, nickname: Optional[str],
                  after: AfterRegister) -> Optional[Target]:
    """입력한 닉네임, 없으면 내 등록 닉네임. 둘 다 없으면 등록 안내"""
    if nickname:
        return validate_nickname(nickname), None, None
    account = accounts.get(interaction.user.id)
    if account:
        return account[1], account[0], interaction.user.id
    prompt = prompt_view(client, after)
    await interaction.response.send_message(view=prompt, ephemeral=True)
    prompt.message = await interaction.original_response()
    return None


class Account(commands.Cog):
    def __init__(self, client: ERClient):
        self.client = client

    @app_commands.command(name="계정", description="내 닉네임 등록")
    @handle_errors(user_message="계정 정보를 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
    async def account_command(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        card = await account_view(self.client, interaction.user.id)
        card.message = await interaction.followup.send(view=card, ephemeral=True, wait=True)


async def setup(client: ERClient):
    await client.add_cog(Account(client))
