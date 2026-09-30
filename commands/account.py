from datetime import datetime
from typing import Awaitable, Callable, Optional, Tuple
from zoneinfo import ZoneInfo

import discord
from discord import app_commands, ui
from discord.ext import commands

from client import ERClient
from utils import accounts, visual
from utils.config import config
from utils.errors import NotFoundError, ValidationError, handle_errors, validate_nickname
from utils.layouts import create_error_layout, CooldownLayoutView

KST = ZoneInfo('Asia/Seoul')

# 닉네임, uid, 등록한 디스코드 유저 ID
Target = Tuple[str, Optional[str], Optional[int]]
AfterRegister = Callable[[discord.Interaction, str, str, int], Awaitable[None]]

HINT = "등록하면 /랭크, /플탐을 닉네임 없이 쓸 수 있습니다."


def account_view(client: ERClient, user_id: int) -> ui.LayoutView:
    view = CooldownLayoutView(timeout=config.view_timeout_interactive)
    account = accounts.get(user_id)
    if account:
        _, nickname, updated = account
        day = datetime.fromtimestamp(updated, KST)
        text = f"### 내 계정\n**{nickname}**\n-# {day.month}/{day.day} 등록 | /랭크, /플탐을 닉네임 없이 쓸 수 있습니다."
        change = ui.Button(style=discord.ButtonStyle.secondary, label="닉네임 변경")
        change.callback = lambda i: i.response.send_modal(RegisterModal(client))
        remove = ui.Button(style=discord.ButtonStyle.danger, label="등록 해제")

        async def unregister(interaction: discord.Interaction):
            accounts.delete(interaction.user.id)
            await interaction.response.edit_message(view=account_view(client, interaction.user.id))

        remove.callback = unregister
        buttons = [change, remove]
    else:
        text = f"### 내 계정\n등록한 닉네임이 없습니다.\n-# {HINT}"
        register = ui.Button(style=discord.ButtonStyle.primary, label="닉네임 등록")
        register.callback = lambda i: i.response.send_modal(RegisterModal(client))
        buttons = [register]
    view.add_item(ui.Container(ui.TextDisplay(text), ui.ActionRow(*buttons), accent_colour=visual.colour('info')))
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
        if not uid:
            await interaction.followup.send(
                view=create_error_layout(f"'{name}' 유저를 찾을 수 없습니다.\n닉네임을 다시 확인해주세요."), ephemeral=True)
            return
        if not accounts.save(interaction.user.id, uid, name):
            await interaction.followup.send(
                view=create_error_layout("닉네임을 저장하지 못했습니다. 잠시 후 다시 시도해주세요."), ephemeral=True)
            return
        await interaction.edit_original_response(view=account_view(self.client, interaction.user.id))
        if self.after:
            await self.after(interaction, name, uid, interaction.user.id)


async def resolve(client: ERClient, interaction: discord.Interaction, nickname: Optional[str],
                  user: Optional[discord.User], after: AfterRegister) -> Optional[Target]:
    """입력한 닉네임, 지정한 유저의 등록 닉네임, 내 등록 닉네임 순. 셋 다 없으면 등록 안내"""
    if nickname and user:
        raise ValidationError("닉네임과 유저 동시 지정", "닉네임과 유저 중 하나만 입력해주세요.")
    if nickname:
        return validate_nickname(nickname), None, None
    target = user or interaction.user
    account = accounts.get(target.id)
    if account:
        return account[1], account[0], target.id
    if user and user.id != interaction.user.id:
        raise NotFoundError("등록 닉네임 없음", f"{user.display_name} 님은 닉네임을 등록하지 않았습니다.")
    await interaction.response.send_message(view=prompt_view(client, after), ephemeral=True)
    return None


class Account(commands.Cog):
    def __init__(self, client: ERClient):
        self.client = client

    @app_commands.command(name="계정", description="내 닉네임 등록")
    @handle_errors(user_message="계정 정보를 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
    async def account_command(self, interaction: discord.Interaction):
        await interaction.response.send_message(view=account_view(self.client, interaction.user.id), ephemeral=True)


async def setup(client: ERClient):
    await client.add_cog(Account(client))
