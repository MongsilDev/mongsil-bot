import asyncio
import discord
from discord.ext import commands
from discord import app_commands, ui
from typing import List, Dict, NamedTuple, Optional
from client import ERClient
from commands.season import get_ranked_season
import math

from utils.config import config
from utils.layouts import create_error_layout, create_loading_layout, CooldownLayoutView
from utils.errors import handle_errors
from utils.logging_config import get_logger
from utils.rank_helpers import RANKING_SERVER, SERVER_NAMES, fetch_user_stats_solo, fetch_ranking_data

logger = get_logger('랭킹')

RANKS_PER_PAGE = 10
TOTAL_RANKS = 100

RANK_MEDALS = {1: '🥇', 2: '🥈', 3: '🥉'}

class RankUser(NamedTuple):
    """랭킹 유저 정보를 저장하는 네임드 튜플"""
    rank: int
    nickname: str
    mmr: int
    user_id: str
    games: int = 0
    wins: int = 0
    avg_rank: float = 0.0
    avg_kills: float = 0.0


def format_user_text(u: RankUser) -> str:
    """개별 유저 텍스트를 포맷합니다."""
    medal = RANK_MEDALS.get(u.rank, f'**#{u.rank}**')
    line = f"{medal}  **{u.nickname}** | **{u.mmr:,}** RP"
    # games 0은 통계 조회 실패
    if u.games > 0:
        line += f"\n-# {u.games}게임 | 승률 {u.wins / u.games * 100:.0f}% | 평균 {u.avg_rank:.1f}등 | 킬 {u.avg_kills:.1f}"
    return line


class PaginationView(CooldownLayoutView):
    def __init__(self, client: ERClient, season_id: int, total_pages: int, first_page_users: List[RankUser], season_name: str):
        super().__init__(timeout=config.view_timeout_interactive)
        self.client = client
        self.season_id = season_id
        self.current_page = 1
        self.total_pages = total_pages
        self.page_cache: Dict[int, List[RankUser]] = {1: first_page_users}
        self._prefetching: Dict[int, asyncio.Task] = {}
        self.season_name = season_name

        self.build_layout()
        self._prefetch(2)

    def _prefetch(self, page: int):
        """페이지당 API 20회라 다음 페이지 선조회"""
        if 1 <= page <= self.total_pages and page not in self.page_cache and page not in self._prefetching:
            self._prefetching[page] = asyncio.create_task(get_ranking_info(self.client, self.season_id, page))

    def build_layout(self):
        """현재 페이지 기준으로 레이아웃을 빌드합니다."""
        self.clear_items()

        users = self.page_cache.get(self.current_page, [])

        children = [ui.TextDisplay(f"### {self.season_name} {SERVER_NAMES[RANKING_SERVER]} 랭킹")]
        children.append(ui.Separator())

        for u in users:
            children.append(ui.TextDisplay(format_user_text(u)))

        self.add_item(ui.Container(*children, accent_colour=discord.Colour.blurple()))

        row = ui.ActionRow(
            ui.Button(label="◀️", style=discord.ButtonStyle.primary, custom_id="prev", disabled=(self.current_page == 1)),
            ui.Button(label=f"{self.current_page}/{self.total_pages}", style=discord.ButtonStyle.secondary, disabled=True, custom_id="indicator"),
            ui.Button(label="▶️", style=discord.ButtonStyle.primary, custom_id="next", disabled=(self.current_page == self.total_pages)),
        )
        self.add_item(row)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """버튼 클릭을 핸들링합니다. (1초 쿨다운 적용)"""
        if not await super().interaction_check(interaction):
            return False

        custom_id = interaction.data.get("custom_id")
        if custom_id == "prev" and self.current_page > 1:
            target_page = self.current_page - 1
        elif custom_id == "next" and self.current_page < self.total_pages:
            target_page = self.current_page + 1
        else:
            await interaction.response.defer()
            return False

        await self.update_page(interaction, target_page)
        return False

    async def update_page(self, interaction: discord.Interaction, target_page: int):
        """페이지를 업데이트합니다. 캐시에 없으면 lazy-load합니다.

        페이지 번호는 로드 성공 후에만 반영한다. 실패한 페이지를 캐시하면
        뷰 수명 동안 그 페이지가 빈 채로 박제되므로 캐시하지 않는다.
        """
        await interaction.response.defer()
        try:
            users = self.page_cache.get(target_page)
            if users is None:
                task = self._prefetching.pop(target_page, None)
                users = await task if task else await get_ranking_info(self.client, self.season_id, target_page)
                if users:
                    self.page_cache[target_page] = users

            if not users:
                await self._send_page_error(interaction)
                return

            self.current_page = target_page
            self.build_layout()
            await interaction.edit_original_response(view=self, attachments=[])
            self._prefetch(target_page + 1)
        except Exception as e:
            logger.error(f"페이지 업데이트 중 오류 발생: {e}", exc_info=True)
            await self._send_page_error(interaction)

    async def _send_page_error(self, interaction: discord.Interaction):
        try:
            layout = create_error_layout("랭킹 페이지를 불러오지 못했습니다. 잠시 후 다시 시도해주세요.")
            await interaction.followup.send(view=layout, ephemeral=True)
        except Exception:
            pass


async def get_ranking_info(client: ERClient, season_id: int, page: int = 1) -> Optional[List[RankUser]]:
    """랭킹 정보를 가져옵니다. 유저별 통계는 병렬로 조회합니다."""
    try:
        ranking_data = await fetch_ranking_data(client, season_id, use_cache=True)
        if ranking_data is None:
            return None

        start_idx = (page - 1) * RANKS_PER_PAGE
        end_idx = min(start_idx + RANKS_PER_PAGE, len(ranking_data))
        page_data = ranking_data[start_idx:end_idx]

        # 10명 동시 요청이면 api_client 세마포어(10)를 독점해 다른 명령이 굶는다
        fetch_limit = asyncio.Semaphore(5)

        async def fetch_single_user(user_data):
            """개별 유저의 통계를 가져옵니다. 실패한 유저는 기본값으로 둔다."""
            nickname = user_data.get('nickname', '')
            user_id = None
            stats = None

            async with fetch_limit:
                if nickname:
                    try:
                        user_id = await client.get_user_nickname(nickname)
                    except Exception:
                        user_id = None

                if user_id:
                    try:
                        stats = await fetch_user_stats_solo(client, user_id, season_id, use_cache=True)
                    except Exception:
                        stats = None

            games = wins = 0
            avg_rank = avg_kills = 0.0

            if stats:
                games = int(stats.get('totalGames', 0))
                wins = int(stats.get('totalWins', 0))
                avg_rank = float(stats.get('averageRank', 0.0))
                avg_kills = float(stats.get('averageKills', 0.0))

            return RankUser(
                rank=user_data.get('rank', 0),
                nickname=nickname,
                mmr=user_data.get('mmr', 0),
                user_id=user_id if user_id else nickname,
                games=games,
                wins=wins,
                avg_rank=avg_rank,
                avg_kills=avg_kills,
            )

        users = await asyncio.gather(*[fetch_single_user(ud) for ud in page_data])
        return list(users)
    except Exception as e:
        logger.error(f"랭킹 정보 처리 중 오류 발생: {e}", exc_info=True)
        return None

class Ranking(commands.Cog):
    def __init__(self, client: ERClient):
        self.client = client

    @app_commands.command(name="랭킹", description="아시아1 상위 100명 순위")
    @handle_errors(user_message="랭킹 정보를 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
    async def ranking_command(self, interaction: discord.Interaction):
        """랭킹을 보여줍니다."""
        await interaction.response.send_message(view=create_loading_layout("랭킹 조회 중"))

        season = await get_ranked_season()
        if not season:
            error_layout = create_error_layout("현재 시즌 정보를 가져올 수 없습니다. 잠시 후 다시 시도해주세요.")
            await interaction.edit_original_response(view=error_layout, embeds=[], attachments=[])
            return
        season_id, season_name = season

        ranking_data = await fetch_ranking_data(self.client, season_id, use_cache=True)
        if not ranking_data:
            error_layout = create_error_layout("랭킹 정보를 가져올 수 없습니다. 잠시 후 다시 시도해주세요.")
            await interaction.edit_original_response(view=error_layout, embeds=[], attachments=[])
            return

        total_pages = math.ceil(min(len(ranking_data), TOTAL_RANKS) / RANKS_PER_PAGE)

        first_page_users = await get_ranking_info(self.client, season_id, 1)
        if not first_page_users:
            error_layout = create_error_layout("랭킹 정보를 가져올 수 없습니다. 잠시 후 다시 시도해주세요.")
            await interaction.edit_original_response(view=error_layout, embeds=[], attachments=[])
            return

        view = PaginationView(self.client, season_id, total_pages, first_page_users, season_name)
        view.message = await interaction.edit_original_response(view=view, embeds=[], attachments=[])

async def setup(client: ERClient):
    """명령어를 등록합니다."""
    await client.add_cog(Ranking(client))
