import sqlite3
import time
from typing import List, Tuple

import discord
from discord import Interaction
from discord.ext import commands
from discord import app_commands, ui
from client import ERClient

from utils.config import config
from utils.layouts import create_error_layout, CooldownLayoutView, mark_shown, sent_message
from utils.errors import handle_errors
from utils.logging_config import get_logger
from utils.emoji_zoom import load_disabled_servers, save_disabled_servers, schedule_webhook_removal
from utils.emojis import EMOJIS
from commands.alerts import channel_ok, hour_label
from utils import guild_settings, usage_db, visual

logger = get_logger('설정')

def guild_usage(guild_id: int) -> List[Tuple[str, int]]:
    since = int(time.time()) - 30 * 86400
    try:
        rows = usage_db.db.execute(
            "SELECT command, COUNT(*) AS n FROM command_log WHERE guild_id = ? AND ts >= ? GROUP BY command ORDER BY n DESC",
            (guild_id, since),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [(r['command'], r['n']) for r in rows]


def scope_text(guild_id: int, prefix: str) -> str:
    mode = guild_settings.get(guild_id, f'{prefix}_mode')
    channels = guild_settings.get(guild_id, f'{prefix}_channels')
    if mode == 'all' or not channels:
        return "모든 채널"
    where = ", ".join(f"<#{c}>" for c in channels[:3]) + (f" 외 {len(channels) - 3}개" if len(channels) > 3 else "")
    return f"{where}만" if mode == 'only' else f"{where} 제외"


class SettingsView(CooldownLayoutView):
    def __init__(self, guild_id: int, channel_id: int = 0):
        super().__init__(timeout=config.view_timeout_interactive)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        gid = self.guild_id
        is_enabled = gid not in load_disabled_servers()

        container = ui.Container(ui.TextDisplay("### 서버 설정"), accent_colour=visual.colour('info'))
        zooms = usage_db.zoom_count(gid)
        zoom_note = ([scope_text(gid, 'zoom')] if is_enabled else []) + ([f"최근 30일 {zooms:,}회"] if zooms else [])
        container.add_item(ui.Section(
            ui.TextDisplay(f"**이모지 확대** {'켜짐' if is_enabled else '꺼짐'}"
                           + (f"\n-# {' | '.join(zoom_note)}" if zoom_note else "")),
            accessory=ui.Button(style=discord.ButtonStyle.danger if is_enabled else discord.ButtonStyle.success,
                                label="끄기" if is_enabled else "켜기", custom_id="toggle_emoji")))
        days = guild_settings.get(gid, 'invite_days')
        container.add_item(ui.TextDisplay(
            f"**명령어 채널** {scope_text(gid, 'command')}\n"
            f"**초대 링크 정리** {f'{days}일 지난 링크 삭제' if days else '꺼짐'}"))

        container.add_item(ui.Separator())
        container.add_item(ui.TextDisplay("### 알림"))
        for key, name, desc in guild_settings.ALERTS:
            current = guild_settings.get(gid, key)
            where = f"<#{current}>" if current else "꺼짐"
            if key == 'alert_cut' and current:
                where += f" {hour_label(guild_settings.get(gid, 'cut_hour'))}"
            here = bool(current) and current == self.channel_id
            button = ui.Button(style=discord.ButtonStyle.secondary,
                               label="끄기" if here else "이 채널로 받기", custom_id=f"alert:{key}")
            container.add_item(ui.Section(ui.TextDisplay(f"**{name}** {where}" + (f"\n-# {desc}" if desc else "")), accessory=button))

        usage = guild_usage(gid)
        if usage:
            total = sum(n for _, n in usage)
            top = " | ".join(f"/{name} {n:,}" for name, n in usage[:3])
            container.add_item(ui.Separator())
            container.add_item(ui.TextDisplay(f"최근 30일 명령어 **{total:,}**회\n-# {top}"))
        self.add_item(container)
        self.add_item(ui.ActionRow(ui.Button(style=discord.ButtonStyle.link, label="대시보드", emoji=EMOJIS['web'],
                                             url=f"{config.dashboard_url}/servers/{gid}")))

    async def refuse(self, interaction: Interaction, text: str):
        await interaction.response.send_message(view=create_error_layout(text), ephemeral=True)

    async def interaction_check(self, interaction: Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False

        custom_id = interaction.data.get("custom_id", "")
        key = custom_id.removeprefix("alert:")
        if custom_id != "toggle_emoji" and key not in guild_settings.ALERT_NAMES:
            await interaction.response.defer()
            return False

        if not interaction.permissions.manage_guild:
            await self.refuse(interaction, "서버 관리 권한이 있는 멤버만 설정을 바꿀 수 있습니다.")
            return False

        if custom_id == "toggle_emoji":
            text = await self.toggle_zoom(interaction)
        else:
            text = await self.set_alert(interaction, key)
        if text is None:
            return False
        guild_settings.log(self.guild_id, text, interaction.user.id, interaction.user.display_name, 'discord')
        # 카드가 새 상태를 바로 보여주므로 별도 완료 안내는 두지 않는다
        self.build_layout()
        await interaction.response.edit_message(view=self)
        return False

    async def toggle_zoom(self, interaction: Interaction):
        # 캐시 원본을 직접 바꾸면 저장 실패 시 메모리만 바뀐 채 남는다
        disabled_servers = set(load_disabled_servers())
        current_enabled = self.guild_id not in disabled_servers
        if current_enabled:
            disabled_servers.add(self.guild_id)
        else:
            if not interaction.guild.me.guild_permissions.manage_webhooks:
                await self.refuse(interaction, "몽실봇에 웹후크 관리 권한이 없어 이모지 확대를 켤 수 없습니다.\n"
                                               "몽실봇 역할에 웹후크 관리 권한을 준 뒤 다시 시도해주세요.")
                return None
            disabled_servers.discard(self.guild_id)
        if not save_disabled_servers(disabled_servers):
            await self.refuse(interaction, "설정을 저장하지 못했습니다.\n잠시 후 다시 시도해주세요.")
            return None
        if current_enabled:
            schedule_webhook_removal(interaction.guild)
        return f"이모지 확대: {'끔' if current_enabled else '켬'}"

    async def set_alert(self, interaction: Interaction, key: str):
        name = guild_settings.ALERT_NAMES[key]
        if guild_settings.get(self.guild_id, key) == self.channel_id:
            value, text = 0, f"{name} 알림: 끔"
        else:
            channel = interaction.channel
            if not isinstance(channel, discord.TextChannel):
                await self.refuse(interaction, "알림은 일반 채널이나 공지 채널에만 보낼 수 있습니다.\n알림을 받을 채널에서 /설정을 다시 실행해주세요.")
                return None
            missing = channel_ok(channel, key == 'alert_cut')
            if missing:
                await self.refuse(interaction, f"몽실봇에 이 채널의 {', '.join(missing)} 권한이 없습니다.\n권한을 준 뒤 다시 시도해주세요.")
                return None
            value, text = channel.id, f"{name} 알림: #{channel.name}"
        if not guild_settings.put(self.guild_id, key, value):
            await self.refuse(interaction, "설정을 저장하지 못했습니다.\n잠시 후 다시 시도해주세요.")
            return None
        return text


class Settings(commands.Cog):
    def __init__(self, client: ERClient):
        self.client = client

    @app_commands.command(name="설정", description="서버 설정")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @handle_errors(user_message="설정을 가져오는 중 오류가 발생했습니다.\n잠시 후 다시 시도해주세요.")
    async def settings_command(self, interaction: discord.Interaction):
        """서버의 봇 설정을 관리합니다."""
        # 서버 연동 설정에서 명령 권한을 넓혀도 설정 카드는 관리자에게만
        if not interaction.permissions.manage_guild:
            await interaction.response.send_message(
                view=create_error_layout("/설정은 서버 관리 권한이 있는 멤버만 쓸 수 있습니다."), ephemeral=True)
            return
        view = SettingsView(interaction.guild_id, interaction.channel_id)
        view.message = await sent_message(interaction, await interaction.response.send_message(view=view, ephemeral=True))
        mark_shown(interaction)

async def setup(client: ERClient):
    """명령어를 등록합니다."""
    await client.add_cog(Settings(client))
