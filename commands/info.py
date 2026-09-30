from datetime import datetime

import discord
from discord import ui
from discord import app_commands
from discord.ext import commands

from client import ERClient
from utils.config import config
from utils.errors import handle_errors
from utils.emojis import EMOJIS, PING_EMOJIS

SERVICE_START = datetime(2023, 6, 15)


def format_uptime(client: ERClient) -> str:
    uptime = client.uptime
    if uptime is None:
        return "0분"
    hours, remainder = divmod(uptime.seconds, 3600)
    parts = []
    if uptime.days:
        parts.append(f"{uptime.days}일")
    if hours:
        parts.append(f"{hours}시간")
    parts.append(f"{remainder // 60}분")
    return " ".join(parts)


def create_bot_info_layout(client: ERClient) -> ui.LayoutView:
    """봇 정보 LayoutView를 생성합니다."""
    days_since_start = (datetime.now() - SERVICE_START).days

    ping_ms = (client.latency or 0.0) * 1000
    if ping_ms < 100:
        ping_emoji = PING_EMOJIS['good']
    elif ping_ms < 200:
        ping_emoji = PING_EMOJIS['normal']
    else:
        ping_emoji = PING_EMOJIS['bad']

    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(
        ui.TextDisplay(f"### 몽실봇\n-# {days_since_start:,}일째 운영 중 | {config.developer_tag}"),
        ui.Separator(),
        ui.TextDisplay(
            f"서버 **{len(client.guilds):,}**개 | "
            f"업타임 **{format_uptime(client)}** | "
            f"{ping_emoji} 핑 **{ping_ms:.0f}**ms"
        ),
        accent_colour=discord.Colour.blurple(),
    ))
    view.add_item(ui.ActionRow(
        ui.Button(
            style=discord.ButtonStyle.link,
            label="지원 서버",
            url=config.support_server,
            emoji=EMOJIS['support'],
        ),
        ui.Button(
            style=discord.ButtonStyle.link,
            label="서버에 추가",
            url=config.bot_invite,
            emoji=EMOJIS['invite'],
        ),
        ui.Button(
            style=discord.ButtonStyle.link,
            label="대시보드",
            url=config.dashboard_url,
            emoji=EMOJIS['web'],
        ),
    ))
    return view


class Info(commands.Cog):
    def __init__(self, client: ERClient):
        self.client = client

    @app_commands.command(
        name="정보",
        description="봇 상태"
    )
    @handle_errors(user_message="봇 정보를 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
    async def info_command(self, interaction: discord.Interaction):
        """봇의 정보를 표시합니다."""
        await interaction.response.send_message(view=create_bot_info_layout(self.client))


async def setup(client: ERClient):
    """명령어를 등록합니다."""
    await client.add_cog(Info(client))
