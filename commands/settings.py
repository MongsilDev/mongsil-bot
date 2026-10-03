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
from utils.emoji_zoom import load_disabled_servers, save_disabled_servers
from utils.emojis import EMOJIS
from utils import usage_db, visual

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


class SettingsView(CooldownLayoutView):
    def __init__(self, guild_id: int):
        super().__init__(timeout=config.view_timeout_interactive)
        self.guild_id = guild_id
        self.build_layout()

    def build_layout(self):
        self.clear_items()
        disabled_servers = load_disabled_servers()
        is_enabled = self.guild_id not in disabled_servers

        status = "켜짐" if is_enabled else "꺼짐"
        status_emoji = EMOJIS['on'] if is_enabled else EMOJIS['off']

        container = ui.Container(accent_colour=visual.colour('info'))
        zooms = usage_db.zoom_count(self.guild_id)
        zoom_line = f"이모지 확대 {status_emoji} **{status}**" + (f"\n-# 최근 30일 {zooms:,}회 확대" if zooms else "")
        container.add_item(ui.TextDisplay(f"### 서버 설정\n{zoom_line}"))
        usage = guild_usage(self.guild_id)
        if usage:
            total = sum(n for _, n in usage)
            top = " | ".join(f"/{name} {n:,}" for name, n in usage[:3])
            container.add_item(ui.Separator())
            container.add_item(ui.TextDisplay(f"최근 30일 사용 **{total:,}**회\n-# {top}"))
        self.add_item(container)

        if is_enabled:
            btn = ui.Button(style=discord.ButtonStyle.danger, label="끄기", custom_id="toggle_emoji")
        else:
            btn = ui.Button(style=discord.ButtonStyle.success, label="켜기", custom_id="toggle_emoji")
        dashboard = ui.Button(style=discord.ButtonStyle.link, label="대시보드", emoji=EMOJIS['web'],
                              url=f"{config.dashboard_url}/servers/{self.guild_id}")
        self.add_item(ui.ActionRow(btn, dashboard))

    async def interaction_check(self, interaction: Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False

        custom_id = interaction.data.get("custom_id")
        if custom_id != "toggle_emoji":
            await interaction.response.defer()
            return False

        if not interaction.user.guild_permissions.manage_guild:
            error_layout = create_error_layout("서버 관리 권한이 있는 유저만 설정을 바꿀 수 있습니다.")
            await interaction.response.send_message(view=error_layout, ephemeral=True)
            return False

        # 토글 로직. 캐시 원본을 직접 바꾸면 저장 실패 시 메모리만 바뀐 채 남는다
        disabled_servers = set(load_disabled_servers())
        current_enabled = self.guild_id not in disabled_servers

        if current_enabled:
            disabled_servers.add(self.guild_id)
        else:
            if not interaction.guild.me.guild_permissions.manage_webhooks:
                error_layout = create_error_layout(
                    "봇에 웹후크 관리 권한이 없어 이모지 확대를 켤 수 없습니다.\n몽실봇 역할에 웹후크 관리 권한을 준 뒤 다시 시도해주세요."
                )
                await interaction.response.send_message(view=error_layout, ephemeral=True)
                return False
            disabled_servers.discard(self.guild_id)

        if save_disabled_servers(disabled_servers):
            # 카드가 새 상태를 바로 보여주므로 별도 완료 안내는 두지 않는다
            self.build_layout()
            await interaction.response.edit_message(view=self)
        else:
            error_layout = create_error_layout("설정을 저장하지 못했습니다. 잠시 후 다시 시도해주세요.")
            await interaction.response.send_message(view=error_layout, ephemeral=True)

        return False

class Settings(commands.Cog):
    def __init__(self, client: ERClient):
        self.client = client

    @app_commands.command(name="설정", description="서버 설정")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @handle_errors(user_message="설정을 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
    async def settings_command(self, interaction: discord.Interaction):
        """서버의 봇 설정을 관리합니다."""
        view = SettingsView(interaction.guild_id)
        view.message = await sent_message(interaction, await interaction.response.send_message(view=view))
        mark_shown(interaction)

async def setup(client: ERClient):
    """명령어를 등록합니다."""
    await client.add_cog(Settings(client))
