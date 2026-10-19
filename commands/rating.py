import asyncio
import time
from datetime import datetime, timezone

import discord
from discord import ui
from discord.ext import commands, tasks
from discord import app_commands
from typing import Dict, List, Optional, Tuple
from client import ERClient
from commands.season import get_ranked_season, get_season_info

from utils.config import config
from utils.layouts import send_card, CooldownLayoutView
from utils.errors import handle_errors, validate_nickname, NotFoundError, ServiceDownError
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

        active = [rank_history.active_cut(season_id, top_ranks, place) for place in (300, 1000)]
        rank_history.record_cuts(season_id, cut_rp(rank_300), cut_rp(rank_1000),
                                 *(max(a[0], TierSystem.RANKED_GATE) if a else None for a in active))
        return rank_300, rank_1000
    except Exception as e:
        logger.error(f"이터컷 조회 실패: {e}", exc_info=True)
        return None, None

async def last_ranked_game(client: ERClient, nickname: str, season_id: int) -> Optional[float]:
    """이 시즌 마지막 랭크 게임 시각. 휴면 기준보다 오래된 기록까지 봤는데 없으면 NO_RECENT,
    일반 게임만 많아 끝까지 못 보면 UNREADABLE, 조회 실패는 None"""
    url = f"{config.api_url}/user/nickname"
    data = await client.api_client.get(url, params={'query': nickname}, use_cache=False)
    user = (data or {}).get('user') or {}
    uid = user.get('userId') or user.get('uid')
    if not uid:
        return None
    limit = time.time() - (rank_history.DORMANT_DAYS + 1) * 86400
    path = f"{config.api_url}/user/games/uid/{uid}"
    for _ in range(15):
        data = await client.api_client.get(path, use_cache=False)
        games = (data or {}).get('userGames') or []
        for g in games:
            if g.get('matchingMode') == 3 and g.get('seasonId') == season_id:
                return datetime.fromisoformat(g['startDtm'].replace('+0900', '+09:00')).timestamp()
        if not games or not data.get('next'):
            return rank_history.NO_RECENT
        if datetime.fromisoformat(games[-1]['startDtm'].replace('+0900', '+09:00')).timestamp() < limit:
            return rank_history.NO_RECENT
        path = f"{config.api_url}/user/games/uid/{uid}?next={data['next']}"
    return rank_history.UNREADABLE


def needs_check(p: list, now: float) -> bool:
    """RP 변화로 최근 활동을 아는 계정은 건너뜀. 모르면 확인, 휴면으로 본 계정은 하루마다 다시 확인"""
    at = rank_history.last_active(p)
    if at is not None and now - at <= rank_history.DORMANT_DAYS * 86400:
        return False
    checked = p[rank_history.CHECKED]
    if checked is None:
        return True
    return now - checked > (86400 if p[rank_history.LAST_GAME] is not None else 1800)


def cut_rp(user: Optional[Dict]) -> Optional[int]:
    """순위 컷 RP. 시즌 초 순위권 점수가 RANKED_GATE보다 낮으면 GATE가 실제 컷"""
    return max(int(user.get('mmr', 0)), TierSystem.RANKED_GATE) if user else None


def _day_ago(history, index: int) -> Optional[int]:
    now = time.time()
    # 봇이 멈춰 기록이 비면 며칠 전 값과 비교하지 않게 28시간까지만
    old = [h for h in history if 20 * 3600 <= now - h[0] <= 28 * 3600 and h[index]]
    return min(old, key=lambda h: abs(now - h[0] - 86400))[index] if old else None


def season_end_for(season_info, season_id: int) -> Optional[datetime]:
    """프리 시즌에는 컷이 직전 정규 시즌 기준이라 현재 시즌 종료일을 붙이지 않음"""
    return season_info.end_date if season_info and season_info.number == season_id else None


def create_rating_layout(rank_300: Optional[Dict], rank_1000: Optional[Dict], season_name: str,
                         season_id: Optional[int] = None, season_end: Optional[datetime] = None,
                         client: Optional[ERClient] = None, top: Optional[List[Dict]] = None) -> ui.LayoutView:
    """레이팅 정보 LayoutView를 생성합니다. top은 아시아1 상위 목록"""
    eternity, demigod = cut_rp(rank_300), cut_rp(rank_1000)
    history = rank_history.cut_history(season_id) if season_id else []
    view = RatingView(client, season_id) if client and season_id else ui.LayoutView()

    ranked = [r for r in top or [] if r.get('mmr')]
    active = {place: rank_history.active_cut(season_id, ranked, place) if season_id and ranked else None
              for place in (300, 1000)}

    def cut_block(tier: str, icon: str, rank: int, rp: Optional[int], index: int) -> str:
        head = f"{app_emojis.tier(icon)} {tier} {rank:,}위".strip()
        if not rp:
            return f"{head} 정보 없음"
        text = f"{head} **{rp:,}** RP"
        before = _day_ago(history, index)
        if before:
            text += f" `24시간 {rp - before:+,}`"
        found = active[rank]
        if found:
            value = max(found[0], TierSystem.RANKED_GATE)
            text += f"\n-# 휴면 제외 {'약 ' if found[2] else ''}**{value:,}** RP, 휴면 {found[1]}명 제외"
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

    if ranked:
        cuts = [(rp, name, visual.TIER_COLOURS[icon]) for rp, name, icon in
                ((eternity, '이터니티', '10'), (demigod, '데미갓', '9')) if rp]
        chart = visual.rp_histogram([int(r['mmr']) for r in ranked[:1000]], cuts)
        if chart:
            children.append(ui.MediaGallery(discord.MediaGalleryItem(visual.attach(view, 'cut_spread.png', chart))))
        by_rank = {r['rank']: int(r['mmr']) for r in ranked}
        facts = [f"{rank}위 {by_rank[rank]:,} RP" for rank in (1, 100, 500) if rank in by_rank]
        if eternity:
            chasing = sum(1 for r in ranked if r['rank'] > 300 and int(r['mmr']) >= eternity - 50)
            facts.append(f"이터니티 컷까지 50 RP 이내 {chasing}명")
        children.append(ui.TextDisplay("-# " + " | ".join(facts)))

    outlook = _outlook(history, active, season_end)
    if outlook:
        lines, chart = outlook
        children.append(ui.TextDisplay(lines))
        children.append(ui.MediaGallery(discord.MediaGalleryItem(visual.attach(view, 'cut_outlook.png', chart))))
        children.append(ui.TextDisplay("-# 휴면은 7일 넘게 랭크 게임 기록이 없는 계정입니다. 예상은 지금의 휴면 제외 컷에 하루 추세를 이어 그린 범위입니다."))

    week_ago = time.time() - 7 * 86400
    recent = [h for h in history if h[0] >= week_ago]
    if not outlook and len(recent) >= 3 and recent[-1][0] - recent[0][0] >= 6 * 3600:
        stamp = lambda ts: datetime.fromtimestamp(ts, timezone.utc)
        chart = visual.lines_chart([
            ([(stamp(h[0]), h[1]) for h in recent], visual.TIER_COLOURS['10']),
            ([(stamp(h[0]), h[2]) for h in recent], visual.TIER_COLOURS['9']),
        ], hours=168 if recent[-1][0] - recent[0][0] > 2 * 86400 else 24, end_labels=True, legend=('이터니티', '데미갓'), mark_extremes=True, label_low=True)
        if chart:
            url = visual.attach(view, 'cut.png', chart)
            children.append(ui.MediaGallery(discord.MediaGalleryItem(url)))

    view.add_item(ui.Container(*children, accent_colour=visual.colour('cut')))
    if isinstance(view, RatingView):
        view.add_item(ui.ActionRow(view.compare_button))
    return view


def _outlook(history, active: Dict[int, Optional[tuple]], season_end: Optional[datetime]):
    """시즌 종료 예상 문구와 차트. 하루 값이 7일 안 되거나 휴면 제외 컷이 없으면 None.
    추세는 휴면 제외 컷 기록이 7일 쌓이기 전까지 순위표 컷 기록으로 계산"""
    if not season_end or not history:
        return None
    now = datetime.now(timezone.utc)
    stamp = lambda ts: datetime.fromtimestamp(ts, timezone.utc)
    rows, bands, series, names = [], [], [], []
    for tier, icon, place, official, own in (('이터니티', '10', 300, 1, 3), ('데미갓', '9', 1000, 2, 4)):
        found = active.get(place)
        if not found:
            continue
        start = max(found[0], TierSystem.RANKED_GATE)
        days = rank_history.daily(history, own)
        if len(days) < 7:
            days = rank_history.daily(history, official)
        span = rank_history.forecast(days, start, now, season_end)
        if not span:
            continue
        low, mid, high = (max(round(v), TierSystem.RANKED_GATE) for v in span)
        colour = visual.TIER_COLOURS[icon]
        rows.append(f"{app_emojis.tier(icon)} {tier} **{low:,}~{high:,}** RP".strip())
        names.append(tier)
        bands.append((now, start, season_end, low, mid, high, colour))
        cutoff = time.time() - 14 * 86400
        series.append(([(stamp(h[0]), h[official]) for h in history if h[0] >= cutoff], colour))
    if not rows:
        return None
    chart = visual.forecast_chart(series, bands, legend=names)
    if not chart:
        return None
    end = season_end.astimezone(visual.KST)
    return f"**시즌 종료 예상** {end.month}/{end.day} 추세 기준\n" + "\n".join(rows), chart


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
    top = await fetch_ranking_data(client, season_id) or []
    mmr = int(stats.get('mmr', 0))
    current = stats.get('nickname') or (user_rank or {}).get('nickname') or name
    mine = accounts.get(interaction.user.id)
    if mine and mine[0] == user_id:
        accounts.rename(interaction.user.id, current)
    lines = [f"### {current}\n**{mmr:,}** RP"]
    for tier, icon, cut, place in (('이터니티', '10', cut_rp(rank_300), 300), ('데미갓', '9', cut_rp(rank_1000), 1000)):
        if not cut:
            continue
        gap = cut - mmr
        state = f"컷까지 **{gap:,}** RP" if gap > 0 else f"컷보다 **{-gap:,}** RP 위"
        lines.append(f"{app_emojis.tier(icon)} {tier} {state}".strip())
        found = rank_history.active_cut(season_id, top, place) if top else None
        if found:
            gap = max(found[0], TierSystem.RANKED_GATE) - mmr
            lines.append(f"-# 휴면 제외 컷까지 {gap:,} RP" if gap > 0 else f"-# 휴면 제외 컷보다 {-gap:,} RP 위")
    if user_rank and user_rank.get('serverCode') not in (None, RANKING_SERVER):
        lines.append(f"-# 컷은 {SERVER_NAMES[RANKING_SERVER]} 기준")
    view = ui.LayoutView()
    view.add_item(ui.Container(ui.TextDisplay("\n".join(lines)), accent_colour=visual.colour('cut')))
    await interaction.followup.send(view=view, ephemeral=True)


class Rating(commands.Cog):
    def __init__(self, client: ERClient):
        self.client = client
        self.warm_ranking.start()

        self.check_activity.start()

    def cog_unload(self):
        self.warm_ranking.cancel()
        self.check_activity.cancel()

    # 휴면 제외 컷용. 상위 1000명 중 활동을 모르는 계정의 마지막 랭크 게임을 1분에 50명씩 확인
    @tasks.loop(minutes=1)
    async def check_activity(self):
        season = await get_ranked_season()
        if not season:
            return
        top = await fetch_ranking_data(self.client, season[0])
        players = rank_history.player_book(season[0])
        now = time.time()
        todo = [(r['nickname'], players[r['nickname']]) for r in top or []
                if r.get('nickname') in players and needs_check(players[r['nickname']], now)][:50]
        if not todo:
            return

        async def one(name: str, p: list):
            try:
                at = await last_ranked_game(self.client, name, season[0])
            except Exception as e:
                logger.debug(f"마지막 랭크 게임 확인 실패 {name}: {type(e).__name__}")
                at = None
            p[rank_history.CHECKED] = now
            if at is not None:
                p[rank_history.LAST_GAME] = at

        await asyncio.gather(*(one(n, p) for n, p in todo))
        rank_history.save()

    @check_activity.before_loop
    async def before_check_activity(self):
        await self.client.wait_until_ready()

    # 랭킹 목록 캐시는 5분. 그 전에 새로 받아 두어 명령이 목록을 기다리지 않게 함
    @tasks.loop(minutes=4)
    async def warm_ranking(self):
        season = await get_ranked_season()
        if not season:
            return
        try:
            await fetch_ranking_data(self.client, season[0], refresh=True)
            await fetch_rating_info(self.client, season[0])
        except Exception as e:
            logger.warning(f"랭킹 목록 미리 받기 실패: {type(e).__name__}: {e}")

    @warm_ranking.before_loop
    async def before_warm_ranking(self):
        await self.client.wait_until_ready()

    @app_commands.command(name="이터컷", description="이터니티와 데미갓 컷")
    @handle_errors(user_message="이터컷을 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
    async def rating_command(self, interaction: discord.Interaction):
        """현재 시즌의 이터니티/데미갓 컷을 확인합니다."""
        async def build():
            season = await get_ranked_season()
            if not season:
                raise ServiceDownError("현재 시즌 조회 실패", "현재 시즌 정보를 가져올 수 없습니다. 잠시 후 다시 시도해주세요.")
            season_id, season_name = season

            (rank_300, rank_1000), season_info, top = await asyncio.gather(
                fetch_rating_info(self.client, season_id), get_season_info(), fetch_ranking_data(self.client, season_id))
            if not rank_300 and not rank_1000:
                raise ServiceDownError(f"{season_name} 이터컷 조회 실패",
                                       f"{season_name} 이터컷을 가져올 수 없습니다. 잠시 후 다시 시도해주세요.")
            return create_rating_layout(rank_300, rank_1000, season_name, season_id,
                                        season_end_for(season_info, season_id), self.client, top)

        view, message = await send_card(interaction, "이터컷 조회 중", build)
        if view:
            view.message = message

async def setup(client: ERClient):
    """명령어를 등록합니다."""
    await client.add_cog(Rating(client))
