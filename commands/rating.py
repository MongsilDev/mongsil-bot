import asyncio
import time
from datetime import datetime, timezone

import discord
from discord import ui
from discord.ext import commands
from discord import app_commands
from typing import Dict, List, Optional, Tuple
from client import ERClient
from commands.season import get_ranked_season, get_season_info

from utils.config import config
from utils.layouts import create_error_layout, CooldownLayoutView
from utils.errors import handle_errors, validate_nickname, NotFoundError
from commands import account
from utils import accounts, app_emojis, rank_history, visual
from utils.logging_config import get_logger
from utils.rank_helpers import RANKING_SERVER, SERVER_NAMES, fetch_ranking_data, fetch_user_rank, fetch_user_stats_solo
from utils.tier_system import TierSystem

logger = get_logger('레이팅')

async def fetch_rating_info(client: ERClient, season_id: int) -> Tuple[Optional[Dict], Optional[Dict]]:
    """300등과 1000등의 유저 정보를 한 번의 API 호출로 가져옵니다."""
    try:
        top_ranks = await fetch_ranking_data(client, season_id, use_cache=True)
        if not top_ranks:
            return None, None

        rank_300 = None
        rank_1000 = None

        # 가져온 데이터에서 300등과 1000등을 찾기
        # API 응답이 순위별로 정렬되어 있다고 가정하고 효율적으로 검색
        for user in top_ranks:
            user_rank = user.get('rank')
            if user_rank == 300:
                rank_300 = user
            elif user_rank == 1000:
                rank_1000 = user

            # 둘 다 찾았으면 루프 종료
            if rank_300 and rank_1000:
                break

        rank_history.record_cuts(season_id, cut_rp(rank_300), cut_rp(rank_1000))
        return rank_300, rank_1000
    except Exception as e:
        logger.error(f"레이팅 정보 조회 중 오류 발생: {e}", exc_info=True)
        return None, None

def cut_rp(user: Optional[Dict]) -> Optional[int]:
    """순위 컷 RP. 시즌 초 순위권 점수가 RANKED_GATE보다 낮으면 GATE가 실제 컷"""
    return max(int(user.get('mmr', 0)), TierSystem.RANKED_GATE) if user else None


def _day_ago(history, index: int) -> Optional[int]:
    now = time.time()
    old = [h for h in history if now - h[0] >= 20 * 3600 and h[index]]
    return min(old, key=lambda h: abs(now - h[0] - 86400))[index] if old else None


def create_rating_layout(rank_300: Optional[Dict], rank_1000: Optional[Dict], season_name: str,
                         season_id: Optional[int] = None, season_end: Optional[datetime] = None,
                         client: Optional[ERClient] = None, top: Optional[List[Dict]] = None) -> ui.LayoutView:
    """레이팅 정보 LayoutView를 생성합니다. top은 아시아1 상위 목록"""
    eternity, demigod = cut_rp(rank_300), cut_rp(rank_1000)
    history = rank_history.cut_history(season_id) if season_id else []
    view = RatingView(client, season_id) if client and season_id else ui.LayoutView()

    def cut_block(tier: str, icon: str, rank: int, rp: Optional[int], index: int) -> str:
        head = f"{app_emojis.tier(icon)} {tier} {rank:,}등".strip()
        if not rp:
            return f"{head} 정보 없음"
        text = f"{head} **{rp:,}** RP"
        before = _day_ago(history, index)
        if before:
            text += f" `24시간 {rp - before:+,}`"
        return text

    sub = [season_name, SERVER_NAMES[RANKING_SERVER]]
    if season_end:
        days = (season_end.date() - datetime.now(season_end.tzinfo).date()).days
        if days >= 0:
            sub.append(f"시즌 종료 D-{days}" if days else "시즌 종료 D-day")
    children = [
        ui.TextDisplay("### 이터컷\n-# " + " | ".join(sub)),
        ui.TextDisplay(cut_block('이터니티', '10', 300, eternity, 1) + "\n" + cut_block('데미갓', '9', 1000, demigod, 2)),
    ]

    ranked = [r for r in top or [] if r.get('mmr')]
    if ranked:
        cuts = [(rp, name, visual.TIER_COLOURS[icon]) for rp, name, icon in
                ((eternity, '이터니티', '10'), (demigod, '데미갓', '9')) if rp]
        chart = visual.rp_histogram([int(r['mmr']) for r in ranked[:1000]], cuts)
        if chart:
            children.append(ui.MediaGallery(discord.MediaGalleryItem(visual.attach(view, 'cut_spread.png', chart))))
        by_rank = {r['rank']: int(r['mmr']) for r in ranked}
        facts = [f"{rank}등 {by_rank[rank]:,}" for rank in (1, 100, 500) if rank in by_rank]
        if eternity:
            chasing = sum(1 for r in ranked if r['rank'] > 300 and int(r['mmr']) >= eternity - 50)
            facts.append(f"이터니티 컷까지 50 RP 이내 {chasing}명")
        children.append(ui.TextDisplay("-# " + " | ".join(facts)))

    week_ago = time.time() - 7 * 86400
    recent = [h for h in history if h[0] >= week_ago]
    if len(recent) >= 3 and recent[-1][0] - recent[0][0] >= 6 * 3600:
        stamp = lambda ts: datetime.fromtimestamp(ts, timezone.utc)
        chart = visual.lines_chart([
            ([(stamp(h[0]), h[1]) for h in recent], visual.TIER_COLOURS['10']),
            ([(stamp(h[0]), h[2]) for h in recent], visual.TIER_COLOURS['9']),
        ], hours=168 if recent[-1][0] - recent[0][0] > 2 * 86400 else 24, end_labels=True, legend=('이터니티', '데미갓'), mark_extremes=True, label_low=True)
        if chart:
            url = visual.attach(view, 'cut.png', chart)
            children.append(ui.MediaGallery(discord.MediaGalleryItem(url)))

    footnote = f"<t:{int(datetime.now(timezone.utc).timestamp())}:t> 기준"
    if eternity and demigod:
        footnote = f"컷 차이 {eternity - demigod:,} RP | {footnote}"
    children.append(ui.TextDisplay(f"-# {footnote}"))

    view.add_item(ui.Container(*children, accent_colour=visual.colour('cut')))
    if isinstance(view, RatingView):
        view.add_item(ui.ActionRow(view.compare_button))
    return view


class RatingView(CooldownLayoutView):
    def __init__(self, client: ERClient, season_id: int):
        super().__init__(timeout=config.view_timeout_interactive)
        self.client = client
        self.season_id = season_id
        self.compare_button = ui.Button(style=discord.ButtonStyle.secondary, label="내 RP와 비교", emoji="🔍")
        self.compare_button.callback = self.open_modal

    async def open_modal(self, interaction: discord.Interaction):
        account = accounts.get(interaction.user.id)
        if account:
            await compare(self.client, self.season_id, interaction, account[1], account[0], interaction.user.id)
        else:
            await interaction.response.send_modal(CompareModal(self.client, self.season_id))


class CompareModal(ui.Modal, title="내 RP와 비교"):
    nickname = ui.TextInput(label="닉네임", max_length=20)

    def __init__(self, client: ERClient, season_id: int):
        super().__init__()
        self.client = client
        self.season_id = season_id

    async def on_submit(self, interaction: discord.Interaction):
        await compare(self.client, self.season_id, interaction, self.nickname.value)


@handle_errors(user_message="RP를 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
async def compare(client: ERClient, season_id: int, interaction: discord.Interaction,
                  name: str, user_id: Optional[str] = None, owner: Optional[int] = None):
    name = name if user_id else validate_nickname(name)
    await interaction.response.defer(ephemeral=True, thinking=True)

    async def load(uid: Optional[str]):
        uid = uid or await client.get_user_nickname(name)
        if not uid:
            raise NotFoundError(f"유저를 찾을 수 없습니다: {name}", f"'{name}' 유저를 찾을 수 없습니다.\n닉네임을 다시 확인해주세요.")
        return (uid, *await asyncio.gather(
            fetch_user_stats_solo(client, uid, season_id),
            fetch_user_rank(client, uid, season_id),
        ))

    user_id, stats, user_rank = await account.with_account(client, owner, name, user_id, load)
    rank_300, rank_1000 = await fetch_rating_info(client, season_id)
    mmr = int(stats.get('mmr', 0))
    current = stats.get('nickname') or (user_rank or {}).get('nickname') or name
    mine = accounts.get(interaction.user.id)
    if mine and mine[0] == user_id:
        accounts.rename(interaction.user.id, current)
    lines = [f"### {current}\n**{mmr:,}** RP"]
    for tier, icon, cut in (('이터니티', '10', cut_rp(rank_300)), ('데미갓', '9', cut_rp(rank_1000))):
        if not cut:
            continue
        gap = cut - mmr
        state = f"컷까지 **{gap:,}** RP" if gap > 0 else f"컷보다 **{-gap:,}** RP 위"
        lines.append(f"{app_emojis.tier(icon)} {tier} {state}".strip())
    if user_rank and user_rank.get('serverCode') not in (None, RANKING_SERVER):
        lines.append(f"-# 컷은 {SERVER_NAMES[RANKING_SERVER]} 기준")
    view = ui.LayoutView()
    view.add_item(ui.Container(ui.TextDisplay("\n".join(lines)), accent_colour=visual.colour('cut')))
    await interaction.followup.send(view=view, ephemeral=True)


class Rating(commands.Cog):
    def __init__(self, client: ERClient):
        self.client = client

    @app_commands.command(name="이터컷", description="이터니티와 데미갓 컷")
    @handle_errors(user_message="레이팅 정보를 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
    async def rating_command(self, interaction: discord.Interaction):
        """현재 시즌의 이터니티/데미갓 컷을 확인합니다."""
        await interaction.response.defer()

        season = await get_ranked_season()
        if not season:
            error_view = create_error_layout("현재 시즌 정보를 가져올 수 없습니다. 잠시 후 다시 시도해주세요.")
            # 공개 defer 뒤 첫 followup이라 ephemeral은 적용되지 않는다
            await interaction.followup.send(view=error_view)
            return
        season_id, season_name = season

        # 레이팅 정보 조회 (한 번의 API 호출로 300등과 1000등 모두 가져오기)
        rank_300, rank_1000 = await fetch_rating_info(self.client, season_id)

        if not rank_300 and not rank_1000:
            error_view = create_error_layout(f"{season_name} 이터컷을 가져올 수 없습니다. 잠시 후 다시 시도해주세요.")
            await interaction.followup.send(view=error_view)
            return

        season_info = await get_season_info()
        top = await fetch_ranking_data(self.client, season_id)
        view = create_rating_layout(rank_300, rank_1000, season_name, season_id,
                                    season_info.end_date if season_info else None, self.client, top)
        view.message = await interaction.followup.send(view=view, files=visual.files_of(view), wait=True)

async def setup(client: ERClient):
    """명령어를 등록합니다."""
    await client.add_cog(Rating(client))
