import os
import platform
import sqlite3
import subprocess
import time
from datetime import datetime
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

import discord
from discord import ui
from discord import app_commands
from discord.ext import commands

from client import ERClient
from utils.config import config
from utils.errors import handle_errors
from utils.layouts import mark_shown
from utils.emojis import EMOJIS, PING_EMOJIS
from utils import usage_db, visual

SERVICE_START = datetime(2023, 6, 15)
KST = ZoneInfo('Asia/Seoul')


UPDATES_CHANNEL = "https://discord.com/channels/1173385068450951269/1173385069507924033"


def _last_update() -> Optional[datetime]:
    """배포본의 마지막 커밋 시각. 폴러가 push를 당기면 봇이 재시작되므로 기동 때 한 번만 읽음"""
    try:
        out = subprocess.run(['git', 'log', '-1', '--format=%ct'], capture_output=True, text=True, timeout=5,
                             cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return datetime.fromtimestamp(int(out.stdout.strip()), KST)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


LAST_UPDATE = _last_update()


def format_uptime(client: ERClient) -> str:
    uptime = client.uptime
    if uptime is None or uptime.total_seconds() < 60:
        return "방금 재시작"
    hours, remainder = divmod(uptime.seconds, 3600)
    parts = []
    if uptime.days:
        parts.append(f"{uptime.days}일")
    if hours:
        parts.append(f"{hours}시간")
    if not uptime.days:
        parts.append(f"{remainder // 60}분")
    return " ".join(parts)


def today_usage() -> Tuple[int, Optional[str]]:
    midnight = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        rows = usage_db.db.execute(
            "SELECT command, COUNT(*) AS n FROM command_log WHERE ts >= ? GROUP BY command ORDER BY n DESC",
            (int(midnight.timestamp()),),
        ).fetchall()
    except sqlite3.Error:
        return 0, None
    return sum(r['n'] for r in rows), rows[0]['command'] if rows else None


def guild_changes(days: int = 30) -> Tuple[int, int]:
    since = int(time.time()) - days * 86400
    try:
        rows = dict(usage_db.db.execute(
            "SELECT kind, COUNT(*) FROM guild_events WHERE ts >= ? GROUP BY kind", (since,)).fetchall())
    except sqlite3.Error:
        return 0, 0
    return rows.get('join', 0), rows.get('leave', 0)


def registered_count() -> int:
    try:
        return usage_db.db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    except sqlite3.Error:
        return 0


def guild_block(guild: discord.Guild) -> Optional[ui.Item]:
    from commands.settings import guild_usage
    from utils.emoji_zoom import load_disabled_servers

    lines = ["### 이 서버", f"멤버 **{guild.member_count or 0:,}**명"]
    joined = guild.me.joined_at if guild.me else None
    if joined:
        joined = joined.astimezone(KST)
        lines[-1] += f" | 봇 추가 {joined.year}/{joined.month}/{joined.day}"
    usage = guild_usage(guild.id)
    if usage:
        top = ", ".join(f"/{name}" for name, _ in usage[:3])
        lines.append(f"최근 30일 명령어 **{sum(n for _, n in usage):,}**회 | {top}")
    zoom = "꺼짐" if guild.id in load_disabled_servers() else "켜짐"
    lines.append(f"이모지 확대 {zoom}")
    return ui.TextDisplay("\n".join(lines))


def create_bot_info_layout(client: ERClient, guild: Optional[discord.Guild] = None) -> ui.LayoutView:
    """봇 정보 LayoutView를 생성합니다. guild가 있으면 그 서버 정보도 함께"""
    days_since_start = (datetime.now() - SERVICE_START).days

    latency = client.latency
    # 첫 하트비트 전에는 nan
    ping_ms = 0.0 if latency != latency else (latency or 0.0) * 1000
    # 게이트웨이가 미국에 있어 한국에서는 정상이어도 180~220ms
    if ping_ms < 250:
        ping_emoji = PING_EMOJIS['good']
    elif ping_ms < 400:
        ping_emoji = PING_EMOJIS['normal']
    else:
        ping_emoji = PING_EMOJIS['bad']

    joins, leaves = guild_changes()
    servers = f"서버 **{len(client.guilds):,}**개"
    if joins or leaves:
        servers += f" `30일 +{joins} -{leaves}`"
    accounts_total = registered_count()
    if accounts_total:
        servers += f" | 닉네임 등록 **{accounts_total:,}**명"
    header = ui.TextDisplay(f"### 몽실봇\n이터널 리턴 정보 봇\n-# {days_since_start:,}일째 운영 중 | {config.developer_tag}")
    user = client.user
    top = ui.Section(header, accessory=ui.Thumbnail(media=user.display_avatar.url)) if user else header
    lines = [
        servers,
        f"업타임 **{format_uptime(client)}** | {ping_emoji} 핑 **{ping_ms:.0f}**ms",
    ]
    runs, favourite = today_usage()
    if runs:
        lines.append(f"오늘 명령어 **{runs:,}**회" + (f" | 많이 쓴 명령어 /{favourite}" if favourite else ""))

    children = [top, ui.Separator(), ui.TextDisplay("\n".join(lines))]
    if guild:
        children += [ui.Separator(), guild_block(guild)]
    footer = [f"discord.py {discord.__version__}", f"Python {platform.python_version()}"]
    if LAST_UPDATE:
        footer.insert(0, f"마지막 업데이트 {LAST_UPDATE.month}/{LAST_UPDATE.day}")
    children.append(ui.TextDisplay("-# " + " | ".join(footer)))

    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*children, accent_colour=visual.colour('info')))
    view.add_item(ui.ActionRow(
        ui.Button(
            style=discord.ButtonStyle.link,
            label="지원 서버",
            url=config.support_server,
            emoji=EMOJIS['support'],
        ),
        ui.Button(
            style=discord.ButtonStyle.link,
            label="업데이트 소식",
            url=UPDATES_CHANNEL,
            emoji=EMOJIS['patch_note'],
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
        await interaction.response.send_message(view=create_bot_info_layout(self.client, interaction.guild))
        mark_shown(interaction)


async def setup(client: ERClient):
    """명령어를 등록합니다."""
    await client.add_cog(Info(client))
