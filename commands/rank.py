import asyncio
from urllib.parse import quote

import discord
from discord import ui
from discord.ext import commands
from discord import app_commands
from typing import Any, Dict, Optional, Tuple
from client import ERClient

from commands.rating import cut_rp, fetch_rating_info
from commands.season import get_ranked_season
from utils.layouts import create_loading_layout
from utils.errors import handle_errors, validate_nickname, NotFoundError, APIError
from utils.logging_config import get_logger
from utils.character_names import get_character_name
from utils.rank_helpers import RANKING_SERVER, SERVER_NAMES, fetch_user_rank, fetch_user_stats_solo
from utils.tier_system import TierSystem
from utils.emojis import EMOJIS

logger = get_logger('랭크')


def next_goal(tier: str, mmr: int, cuts: Tuple[Optional[int], Optional[int]]) -> str:
    """다음 티어까지 남은 RP 문구. 목표가 없으면 빈 문자열"""
    eternity_cut, demigod_cut = cuts
    if tier == "데미갓":
        target, label = eternity_cut, "이터니티 컷"
    elif tier == "미스릴":
        target, label = demigod_cut or TierSystem.RANKED_GATE, "데미갓 컷"
    else:
        step = TierSystem.next_rp_tier(tier)
        if not step:
            return ""
        label, target = step
    if not target or target <= mmr:
        return ""
    return f"{label}까지 {target - mmr:,} RP"


def create_rank_layout(
    nickname: str,
    stats: Dict[str, Any],
    user_rank: Optional[Dict[str, Any]],
    season_name: str,
    cuts: Tuple[Optional[int], Optional[int]] = (None, None),
) -> ui.LayoutView:
    """랭크 정보 LayoutView를 생성합니다."""
    mmr = int(stats.get('mmr', 0))
    games = int(stats.get('totalGames', 0))
    wins = int(stats.get('totalWins', 0))
    win_rate = (wins / games * 100) if games > 0 else 0.0
    actual_nickname = stats.get('nickname', nickname)

    # 이터니티와 데미갓은 귀속 서버 순위 기준. 통계의 rank는 통합 순위라 서버 컷과 어긋남
    server_rank = int(user_rank.get('serverRank', 0)) if user_rank else 0
    tier = TierSystem.get_tier(mmr, server_rank or int(stats.get('rank', 0)))

    if server_rank and server_rank <= 1000:
        server = SERVER_NAMES.get(user_rank.get('serverCode'), "서버")
        place = f"{server} {server_rank:,}등"
    else:
        rank = int(stats.get('rank', 0))
        rank_size = int(stats.get('rankSize', 0))
        place = f"상위 {rank / rank_size * 100:.2f}%" if rank_size else ""

    icon_url = f"https://cdn.mongsil.dev/mongsilbot/tier2/{TierSystem.get_tier_icon(tier)}.png"
    header_text = (
        f"## {actual_nickname}\n"
        f"{tier} | **{mmr:,}** RP\n"
        f"-# {season_name}" + (f" | {place}" if place else "")
    )
    goal = next_goal(tier, mmr, cuts)
    if goal:
        header_text += f"\n-# {goal}"

    # 탑1은 솔로 승률과 같은 지표라 표시하지 않는다
    stats_text = (
        f"**{games:,}**게임 | 승률 **{win_rate:.0f}%** | 탑3 **{float(stats.get('top3', 0.0)) * 100:.0f}%**\n"
        f"평균 **{float(stats.get('averageRank', 0.0)):.1f}**등 | "
        f"킬 **{float(stats.get('averageKills', 0.0)):.1f}** | "
        f"어시 **{float(stats.get('averageAssistants', 0.0)):.1f}**"
    )

    container_items = [
        ui.Section(ui.TextDisplay(header_text), accessory=ui.Thumbnail(media=icon_url)),
        ui.Separator(),
        ui.TextDisplay(stats_text),
    ]

    top_characters = sorted(stats.get('characterStats') or [], key=lambda x: x.get('totalGames', 0), reverse=True)[:3]
    if top_characters:
        char_lines = []
        for char in top_characters:
            char_games = char.get('totalGames', 0)
            char_win = (char.get('wins', 0) / char_games * 100) if char_games else 0.0
            char_top3 = (char.get('top3', 0) / char_games * 100) if char_games else 0.0
            char_lines.append(
                f"**{get_character_name(char.get('characterCode', 0))}** "
                f"{char_games}게임 | 승률 {char_win:.0f}% | 탑3 {char_top3:.0f}%"
            )
        container_items.append(ui.Separator())
        container_items.append(ui.TextDisplay("### 모스트 캐릭터\n" + "\n".join(char_lines)))

    view = ui.LayoutView()
    view.add_item(ui.Container(*container_items, accent_colour=discord.Colour.blurple()))
    view.add_item(ui.ActionRow(
        ui.Button(
            style=discord.ButtonStyle.link,
            label="DAK.GG",
            emoji=EMOJIS['chart'],
            url=f"https://dak.gg/er/players/{quote(actual_nickname)}"
        )
    ))
    return view


async def build_rank_view(client: ERClient, nickname: str) -> ui.LayoutView:
    """닉네임으로 랭크 카드를 만든다. 명령과 웹 미리보기가 같이 씀"""
    season = await get_ranked_season()
    if not season:
        raise APIError("시즌 정보를 가져올 수 없습니다.", "현재 시즌 정보를 가져올 수 없습니다.\n잠시 후 다시 시도해주세요.")
    season_id, season_name = season

    user_id = await client.get_user_nickname(nickname)
    if not user_id:
        raise NotFoundError(
            f"유저를 찾을 수 없습니다: {nickname}",
            f"'{nickname}' 유저를 찾을 수 없습니다.\n닉네임을 다시 확인해주세요."
        )

    stats, user_rank = await asyncio.gather(
        fetch_user_stats_solo(client, user_id, season_id, use_cache=True),
        fetch_user_rank(client, user_id, season_id),
    )

    # 순위 컷은 아시아1 목록만 있어 다른 서버 유저는 컷 목표를 생략
    cuts = (None, None)
    if (user_rank and user_rank.get('serverCode') == RANKING_SERVER
            and int(stats.get('mmr', 0)) >= TierSystem.TIERS["미스릴"]["base"]):
        rank_300, rank_1000 = await fetch_rating_info(client, season_id)
        cuts = (cut_rp(rank_300), cut_rp(rank_1000))

    return create_rank_layout(nickname, stats, user_rank, season_name, cuts)


class Rank(commands.Cog):
    def __init__(self, client: ERClient):
        self.client = client

    @app_commands.command(name="랭크", description="시즌 랭크 전적")
    @app_commands.describe(닉네임="이터널 리턴 닉네임")
    @handle_errors(user_message="랭크 정보를 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
    async def rank_command(self, interaction: discord.Interaction, 닉네임: str):
        """유저의 랭크 정보를 조회합니다."""
        # 입력 검증 (실패 시 handle_errors가 user_message를 ephemeral로 전송)
        validated_nickname = validate_nickname(닉네임)

        await interaction.response.send_message(view=create_loading_layout("랭크 조회 중"))
        view = await build_rank_view(self.client, validated_nickname)
        await interaction.edit_original_response(view=view, embeds=[], attachments=[])

async def setup(client: ERClient):
    """명령어를 등록합니다."""
    await client.add_cog(Rank(client))
