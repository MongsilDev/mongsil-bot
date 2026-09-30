import asyncio
from urllib.parse import quote

import discord
from discord import ui
from discord.ext import commands
from discord import app_commands
from typing import Any, Dict, List, Optional, Tuple
from client import ERClient

from commands.rating import cut_rp, fetch_rating_info
from commands.season import get_ranked_season
from utils.config import config
from utils.layouts import create_loading_layout, send_card, CooldownLayoutView
from commands import account
from utils import accounts, app_emojis, visual
from utils.errors import handle_errors, validate_nickname, NotFoundError, APIError
from utils.logging_config import get_logger
from utils.character_names import get_character_name
from utils.rank_helpers import RANKING_SERVER, SERVER_NAMES, fetch_user_rank, fetch_user_stats_solo
from utils.tier_system import TierSystem
from utils.emojis import EMOJIS

logger = get_logger('랭크')


RECENT_GAMES = 20


def next_goal(tier: str, mmr: int, cuts: Tuple[Optional[int], Optional[int]]) -> Optional[Tuple[str, int, float]]:
    """다음 목표 이름, 남은 RP, 현재 구간 진행률. 목표가 없으면 None"""
    eternity_cut, demigod_cut = cuts
    if tier == "데미갓":
        target, label = eternity_cut, "이터니티 컷"
        floor = demigod_cut or TierSystem.RANKED_GATE
    elif tier == "미스릴":
        target, label = demigod_cut or TierSystem.RANKED_GATE, "데미갓 컷"
        floor = TierSystem.TIERS["미스릴"]["base"]
    else:
        step = TierSystem.next_rp_tier(tier)
        if not step:
            return None
        label, target = step
        floor = TierSystem.TIERS[tier]["base"]
    if not target or target <= mmr:
        return None
    ratio = (mmr - floor) / (target - floor) if target > floor else 0.0
    return label, target - mmr, min(max(ratio, 0.0), 1.0)


async def fetch_recent_ranked(client: ERClient, user_id: str, season_id: int) -> List[Dict[str, Any]]:
    """이번 시즌 최근 랭크 게임, 최신순. 실패하면 빈 목록"""
    games: List[Dict[str, Any]] = []
    url = f"{config.api_url}/user/games/uid/{user_id}"
    try:
        for _ in range(2):
            data = await client.api_client.get(url, ttl=300)
            if not data:
                break
            for game in data.get('userGames', []):
                if game.get('matchingMode') == 3 and game.get('seasonId') == season_id and game.get('mmrAfter'):
                    games.append(game)
            if len(games) >= RECENT_GAMES or not data.get('next'):
                break
            url = f"{config.api_url}/user/games/uid/{user_id}?next={data['next']}"
    except Exception as e:
        logger.warning(f"최근 게임 조회 실패, 흐름 없이 표시: {e}")
    return games[:RECENT_GAMES]


def create_rank_layout(
    nickname: str,
    stats: Dict[str, Any],
    user_rank: Optional[Dict[str, Any]],
    season_name: str,
    cuts: Tuple[Optional[int], Optional[int]] = (None, None),
    recent: Optional[List[Dict[str, Any]]] = None,
    client: Optional[ERClient] = None,
) -> ui.LayoutView:
    """랭크 정보 LayoutView를 생성합니다."""
    mmr = int(stats.get('mmr', 0))
    games = int(stats.get('totalGames', 0))
    wins = int(stats.get('totalWins', 0))
    win_rate = (wins / games * 100) if games > 0 else 0.0
    actual_nickname = stats.get('nickname') or (user_rank or {}).get('nickname') or nickname

    # 이터니티와 데미갓은 귀속 서버 순위 기준. 통계의 rank는 통합 순위라 서버 컷과 어긋남
    server_rank = int(user_rank.get('serverRank', 0)) if user_rank else 0
    tier = TierSystem.get_tier(mmr, server_rank or int(stats.get('rank', 0)))
    icon = TierSystem.get_tier_icon(tier)

    if server_rank and server_rank <= 1000:
        server = SERVER_NAMES.get(user_rank.get('serverCode'), "서버")
        place = f"{server} {server_rank:,}등"
    else:
        rank = int(stats.get('rank', 0))
        rank_size = int(stats.get('rankSize', 0))
        place = f"상위 {rank / rank_size * 100:.2f}%" if rank_size else ""

    view = RankView(client, actual_nickname) if client else ui.LayoutView()
    icon_url = f"https://cdn.mongsil.dev/mongsilbot/tier2/{icon}.png"
    header_lines = [
        f"## {actual_nickname}",
        f"{tier} **{mmr:,}** RP",
        f"-# {season_name}" + (f" | {place}" if place else ""),
    ]
    goal = next_goal(tier, mmr, cuts)
    if goal:
        label, left, ratio = goal
        header_lines.append(f"`{visual.gauge(ratio, 10)}` {label}까지 **{left:,}** RP")
    container_items = [ui.Section(ui.TextDisplay("\n".join(header_lines)), accessory=ui.Thumbnail(media=icon_url))]

    # 탑1은 솔로 승률과 같은 지표라 표시하지 않는다
    stats_text = (
        f"**{games:,}**게임 | 승률 **{win_rate:.0f}%** | "
        f"평균 **{float(stats.get('averageRank', 0.0)):.1f}**등 | "
        f"킬 **{float(stats.get('averageKills', 0.0)):.1f}** | "
        f"어시 **{float(stats.get('averageAssistants', 0.0)):.1f}**"
    )
    container_items += [ui.Separator(), ui.TextDisplay(stats_text)]

    if recent:
        oldest_first = list(reversed(recent))
        places = [int(g.get('gameRank', 0)) for g in oldest_first]
        gain = int(oldest_first[-1]['mmrAfter']) - int(oldest_first[0].get('mmrBefore') or oldest_first[0]['mmrAfter'])
        chart = visual.rp_chart(
            int(oldest_first[0].get('mmrBefore') or oldest_first[0]['mmrAfter']),
            [(int(g['mmrAfter']), p) for g, p in zip(oldest_first, places)],
            visual.TIER_COLOURS.get(icon, visual.TIER_COLOURS['0']),
        )
        container_items.append(ui.TextDisplay(
            f"-# 최근 {len(recent)}게임 | 평균 {sum(places) / len(places):.1f}등 | RP {gain:+,}"
        ))
        if chart:
            url = visual.attach(view, 'rank.png', chart)
            container_items.append(ui.MediaGallery(discord.MediaGalleryItem(url)))

    top_characters = sorted(stats.get('characterStats') or [], key=lambda x: x.get('totalGames', 0), reverse=True)[:3]
    if top_characters:
        parts = []
        for char in top_characters:
            code = char.get('characterCode', 0)
            char_games = char.get('totalGames', 0)
            char_win = (char.get('wins', 0) / char_games * 100) if char_games else 0.0
            face = app_emojis.character(code) or f"**{get_character_name(code)}**"
            parts.append(f"{face} {char_games}게임 승률 {char_win:.0f}%")
        container_items.append(ui.Separator())
        container_items.append(ui.TextDisplay(" | ".join(parts)))

    view.add_item(ui.Container(*container_items, accent_colour=visual.tier_colour(icon)))
    row = ui.ActionRow(
        ui.Button(
            style=discord.ButtonStyle.link,
            label="DAK.GG",
            emoji=EMOJIS['chart'],
            url=f"https://dak.gg/er/players/{quote(actual_nickname)}"
        )
    )
    if client:
        row.add_item(view.playtime_button)
    view.add_item(row)
    return view


class RankView(CooldownLayoutView):
    """플탐 버튼이 있는 랭크 카드"""

    def __init__(self, client: ERClient, nickname: str):
        super().__init__(timeout=config.view_timeout_interactive)
        self.client = client
        self.nickname = nickname
        self.playtime_button = ui.Button(style=discord.ButtonStyle.secondary, label="플탐", emoji=EMOJIS['clock'])
        self.playtime_button.callback = self.show_playtime

    @handle_errors(user_message="플레이 타임 정보를 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
    async def show_playtime(self, interaction: discord.Interaction):
        from commands.playtime import build_playtime_view
        await interaction.response.send_message(view=create_loading_layout("플레이 타임 조회 중"))
        view = await build_playtime_view(self.client, self.nickname)
        await interaction.edit_original_response(view=view, attachments=visual.files_of(view))


async def build_rank_view(client: ERClient, nickname: str, buttons: bool = True,
                          user_id: Optional[str] = None) -> ui.LayoutView:
    """닉네임으로 랭크 카드를 만든다. 명령과 웹 미리보기가 같이 씀"""
    season = await get_ranked_season()
    if not season:
        raise APIError("시즌 정보를 가져올 수 없습니다.", "현재 시즌 정보를 가져올 수 없습니다.\n잠시 후 다시 시도해주세요.")
    season_id, season_name = season

    user_id = user_id or await client.get_user_nickname(nickname)
    if not user_id:
        raise NotFoundError(
            f"유저를 찾을 수 없습니다: {nickname}",
            f"'{nickname}' 유저를 찾을 수 없습니다.\n닉네임을 다시 확인해주세요."
        )

    stats, user_rank, recent = await asyncio.gather(
        fetch_user_stats_solo(client, user_id, season_id, use_cache=True),
        fetch_user_rank(client, user_id, season_id),
        fetch_recent_ranked(client, user_id, season_id),
    )

    # 순위 컷은 아시아1 목록만 있어 다른 서버 유저는 컷 목표를 생략
    cuts = (None, None)
    if (user_rank and user_rank.get('serverCode') == RANKING_SERVER
            and int(stats.get('mmr', 0)) >= TierSystem.TIERS["미스릴"]["base"]):
        rank_300, rank_1000 = await fetch_rating_info(client, season_id)
        cuts = (cut_rp(rank_300), cut_rp(rank_1000))

    return create_rank_layout(nickname, stats, user_rank, season_name, cuts, recent, client if buttons else None)


class Rank(commands.Cog):
    def __init__(self, client: ERClient):
        self.client = client

    @app_commands.command(name="랭크", description="시즌 랭크 전적")
    @app_commands.describe(닉네임="이터널 리턴 닉네임, 비우면 내 닉네임", 유저="닉네임을 등록한 디스코드 유저")
    @handle_errors(user_message="랭크 정보를 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
    async def rank_command(self, interaction: discord.Interaction,
                           닉네임: Optional[str] = None, 유저: Optional[discord.User] = None):
        """유저의 랭크 정보를 조회합니다."""
        target = await account.resolve(self.client, interaction, 닉네임, 유저, self.show)
        if target:
            await self.show(interaction, *target)

    @handle_errors(user_message="랭크 정보를 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
    async def show(self, interaction: discord.Interaction, nickname: str, uid: Optional[str], owner: Optional[int]):
        view, message = await send_card(interaction, "랭크 조회 중",
                                        lambda: account.with_account(self.client, owner, nickname, uid,
                                                                     lambda u: build_rank_view(self.client, nickname, user_id=u)))
        if view is None:
            return
        view.message = message
        if owner and getattr(view, 'nickname', None):
            accounts.rename(owner, view.nickname)

async def setup(client: ERClient):
    """명령어를 등록합니다."""
    await client.add_cog(Rank(client))
