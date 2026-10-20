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

        end = season_end_for(await get_season_info(), season_id)
        active = [rank_history.active_cut(season_id, top_ranks, place, end and end.timestamp()) for place in (300, 1000)]
        rps = [int(r['mmr']) for r in sorted(top_ranks, key=lambda r: r.get('rank', 0)) if r.get('mmr')]
        rank_history.record_cuts(season_id, cut_rp(rank_300), cut_rp(rank_1000),
                                 *(max(a[0], TierSystem.RANKED_GATE) if a else None for a in active), rps=rps)
        return rank_300, rank_1000
    except Exception as e:
        logger.error(f"이터컷 조회 실패: {e}", exc_info=True)
        return None, None

async def last_ranked_game(client: ERClient, nickname: str, season_id: int,
                           uid: Optional[str] = None) -> Tuple[Optional[str], Optional[float]]:
    """uid와 이 시즌 마지막 랭크 게임 시각. 게임 기록 끝까지 없으면 NO_RECENT,
    일반 게임만 많아 끝까지 읽지 못하면 UNREADABLE, 조회 실패는 None"""
    if not uid:
        data = await client.api_client.get(f"{config.api_url}/user/nickname", params={'query': nickname}, use_cache=False)
        user = (data or {}).get('user') or {}
        uid = user.get('userId') or user.get('uid')
        if not uid:
            return None, None
    path = f"{config.api_url}/user/games/uid/{uid}"
    for _ in range(15):
        data = await client.api_client.get(path, use_cache=False)
        games = (data or {}).get('userGames') or []
        for g in games:
            if g.get('matchingMode') == 3 and g.get('seasonId') == season_id:
                return uid, datetime.fromisoformat(g['startDtm'].replace('+0900', '+09:00')).timestamp()
        if not games or not data.get('next'):
            return uid, rank_history.NO_RECENT
        path = f"{config.api_url}/user/games/uid/{uid}?next={data['next']}"
    return uid, rank_history.UNREADABLE


def needs_check(p: list, now: float) -> bool:
    """RP 변화로 최근 활동을 아는 계정은 건너뜀. 모르면 확인, 휴면으로 판단한 계정은 하루마다 다시 확인"""
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


def tier_cuts(season_id: Optional[int], ranked: List[Dict], history: List[tuple], season_end: Optional[datetime],
              eternity: Optional[int], demigod: Optional[int], now: datetime) -> List[tuple]:
    """티어마다 (이름, 아이콘, 순위, 순위표 컷, 기록 열, 휴면 강등 컷 결과, 표시 컷, 종료 예상)"""
    end = season_end.timestamp() if season_end else None
    tiers = []
    for tier, icon, place, rp, official in (('이터니티', '10', 300, eternity, 1), ('데미갓', '9', 1000, demigod, 2)):
        found = rank_history.active_cut(season_id, ranked, place, end) if season_id and ranked else None
        start = max(found[0], TierSystem.RANKED_GATE) if found else rp
        outlook = None
        if start and season_end:
            outlook = rank_history.forecast(rank_history.at_nine(history, official), start, now, season_end,
                                            rank_history.SURGE[place])
        tiers.append((tier, icon, place, rp, official, found, start, outlook))
    return tiers


async def cut_snapshot(client: ERClient) -> Optional[dict]:
    """labs.mongsil.dev 이터컷 예측 페이지용. /이터컷과 같은 계산. 휴면 계정은 인원만 내보냄"""
    season = await get_ranked_season()
    if not season:
        return None
    season_id, season_name = season
    ranked = [r for r in await fetch_ranking_data(client, season_id) or [] if r.get('mmr')]
    if not ranked:
        return None
    rank_300, rank_1000 = await fetch_rating_info(client, season_id)
    end = season_end_for(await get_season_info(), season_id)
    history = rank_history.cut_history(season_id)
    now = datetime.now(timezone.utc)
    stamp = lambda t: round(t.timestamp()) if isinstance(t, datetime) else round(t)
    tiers = {}
    for key, (tier, icon, place, rp, official, found, start, outlook) in zip(
            ('eternity', 'demigod'), tier_cuts(season_id, ranked, history, end, cut_rp(rank_300), cut_rp(rank_1000), now)):
        board = {stamp(t): v for t, v in rank_history.at_nine(history, official)}
        adjusted = {stamp(t): v for t, v in rank_history.at_nine(history, official + 2)}
        days = sorted(board)[-30:]
        schedule, stay = rank_history.decay_schedule(season_id, ranked, start, end.timestamp()) if end and start else ([], 0)
        tiers[key] = {
            'place': place, 'board': rp, 'cut': start, 'estimated': bool(found and found[2]),
            'dropped': found[1] if found else None,
            'history': [[t, board[t], adjusted.get(t)] for t in days],
            'forecast': [[stamp(t), round(m), round(lo), round(hi), round(raw)]
                         for (t, m, lo, hi), raw in zip(outlook['future'], outlook['raw_low'])] if outlook else None,
            'decay': [[d, n] for d, n in schedule], 'decayStay': stay,
        }
    return {
        'updatedAt': round(now.timestamp()),
        'season': {'id': season_id, 'name': season_name, 'end': stamp(end) if end else None},
        'rps': [int(r['mmr']) for r in sorted(ranked, key=lambda r: r.get('rank', 0))],
        'tiers': tiers,
        # 지금부터 랭크 게임을 안 하면 시즌 끝까지 깎이는 횟수. 유예 12 가정
        'quitTicks': rank_history.decay_ticks(now.timestamp(), end.timestamp()) if end else None,
        'rule': {'grace': rank_history.GRACE, 'step': 100, 'floor': rank_history.DECAY_FLOOR, 'dormantDays': rank_history.DORMANT_DAYS},
    }


def create_rating_layout(rank_300: Optional[Dict], rank_1000: Optional[Dict], season_name: str,
                         season_id: Optional[int] = None, season_end: Optional[datetime] = None,
                         client: Optional[ERClient] = None, top: Optional[List[Dict]] = None) -> ui.LayoutView:
    """레이팅 정보 LayoutView를 생성합니다. top은 아시아1 상위 목록"""
    history = rank_history.cut_history(season_id) if season_id else []
    ranked = [r for r in top or [] if r.get('mmr')]
    now = datetime.now(timezone.utc)
    tiers = tier_cuts(season_id, ranked, history, season_end, cut_rp(rank_300), cut_rp(rank_1000), now)
    view = RatingView(client, season_id, ranked, *(t[6] for t in tiers)) if client and season_id else ui.LayoutView()

    def block(tier, icon, place, rp, official, found, start, outlook) -> str:
        head = f"{app_emojis.tier(icon)} {tier} {place:,}위".strip()
        if not start:
            return f"{head} 정보 없음"
        text = f"{head} {'약 ' if found and found[2] else ''}**{start:,}** RP"
        before = _day_ago(history, official + 2 if found else official)
        if before:
            text += f" `24시간 {start - before:+,}`"
        if not found:
            text += "\n-# 휴면 확인 중이라 순위표 기준"
        return text

    sub = [season_name, SERVER_NAMES[RANKING_SERVER]]
    if season_end:
        days = (season_end.date() - datetime.now(season_end.tzinfo).date()).days
        if days >= 0:
            sub.append(f"시즌 종료 D-{days}" if days else "시즌 종료 D-day")
    children = [
        ui.TextDisplay("### 이터컷\n-# " + " | ".join(sub)),
        ui.TextDisplay("\n".join(block(*t) for t in tiers)),
    ]

    chart = _outlook_chart(history, tiers, now) if season_end else None
    if chart:
        children.append(ui.MediaGallery(discord.MediaGalleryItem(visual.attach(view, 'cut_outlook.png', chart))))
    notes = []
    if any(t[5] for t in tiers):
        notes.append("휴면 강등 반영")
    if chart:
        notes.append("막판 급등 반영")
    if notes:
        children.append(ui.TextDisplay("-# " + " | ".join(notes)))
    if not chart:
        week_ago = time.time() - 7 * 86400
        recent = [h for h in history if h[0] >= week_ago]
        if len(recent) >= 3 and recent[-1][0] - recent[0][0] >= 6 * 3600:
            stamp = lambda ts: datetime.fromtimestamp(ts, timezone.utc)
            chart = visual.lines_chart([
                ([(stamp(h[0]), h[1]) for h in recent], visual.TIER_COLOURS['10']),
                ([(stamp(h[0]), h[2]) for h in recent], visual.TIER_COLOURS['9']),
            ], hours=168 if recent[-1][0] - recent[0][0] > 2 * 86400 else 24, end_labels=True, legend=('이터니티', '데미갓'), mark_extremes=True, label_low=True)
            if chart:
                children.append(ui.MediaGallery(discord.MediaGalleryItem(visual.attach(view, 'cut.png', chart))))

    view.add_item(ui.Container(*children, accent_colour=visual.colour('cut')))
    if isinstance(view, RatingView):
        view.add_item(ui.ActionRow(view.compare_button, view.spread_button))
    return view


def _outlook_chart(history, tiers, now: datetime) -> Optional[bytes]:
    """최근 2주 휴면 강등 컷과 시즌 종료까지 날마다 오전 9시 예상 범위. 예상이 하나도 없으면 None"""
    bands, series, names = [], [], []
    for tier, icon, _, rp, official, _, start, outlook in tiers:
        if not outlook:
            continue
        colour = visual.TIER_COLOURS[icon]
        future = [(t, *(max(v, TierSystem.RANKED_GATE) for v in vals)) for t, *vals in outlook['future']]
        bands.append((now, start, future, colour))
        own = [p for p in rank_history.at_nine(history, official + 2)[-14:] if p[0] < now]
        series.append((own + [(now, start)], colour))
        names.append(tier)
    return visual.forecast_chart(series, bands, legend=names) if bands else None


def spread_layout(ranked: List[Dict], eternity: Optional[int], demigod: Optional[int]) -> ui.LayoutView:
    """상위 1000명 RP 분포와 주요 순위 RP"""
    view = ui.LayoutView()
    children = [ui.TextDisplay("### 상위 1000명 RP 분포")]
    cuts = [(rp, name, visual.TIER_COLOURS[icon]) for rp, name, icon in
            ((eternity, '이터니티', '10'), (demigod, '데미갓', '9')) if rp]
    chart = visual.rp_histogram([int(r['mmr']) for r in ranked[:1000]], cuts)
    if chart:
        children.append(ui.MediaGallery(discord.MediaGalleryItem(visual.attach(view, 'cut_spread.png', chart))))
    by_rank = {r['rank']: int(r['mmr']) for r in ranked}
    facts = [f"{rank}위 {by_rank[rank]:,} RP" for rank in (1, 100, 500) if rank in by_rank]
    if eternity:
        near = [int(r['mmr']) - eternity for r in ranked]
        facts.append(f"이터니티 컷 위 50 RP 이내 {sum(0 <= g < 50 for g in near)}명, 아래 50 RP 이내 {sum(-50 <= g < 0 for g in near)}명")
    children.append(ui.TextDisplay("-# " + " | ".join(facts)))
    view.add_item(ui.Container(*children, accent_colour=visual.colour('cut')))
    return view


class RatingView(CooldownLayoutView):
    def __init__(self, client: ERClient, season_id: int, ranked: List[Dict] = (),
                 eternity: Optional[int] = None, demigod: Optional[int] = None):
        super().__init__(timeout=config.view_timeout_interactive)
        self.client = client
        self.season_id = season_id
        self.spread = (list(ranked), eternity, demigod)
        self.compare_button = ui.Button(style=discord.ButtonStyle.secondary, label="내 RP와 비교", emoji="🔍")
        self.compare_button.callback = self.open_modal
        self.spread_button = ui.Button(style=discord.ButtonStyle.secondary, label="RP 분포", emoji="📊",
                                       disabled=not ranked)
        self.spread_button.callback = self.show_spread

    async def show_spread(self, interaction: discord.Interaction):
        view = spread_layout(*self.spread)
        await interaction.response.send_message(view=view, files=visual.files_of(view), ephemeral=True)

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


@handle_errors(user_message="RP를 가져오는 중 오류가 발생했습니다.\n잠시 후 다시 시도해주세요.")
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
    ranked = [r for r in await fetch_ranking_data(client, season_id) or [] if r.get('mmr')]
    end = season_end_for(await get_season_info(), season_id)
    mmr = int(stats.get('mmr', 0))
    current = stats.get('nickname') or (user_rank or {}).get('nickname') or name
    mine = accounts.get(interaction.user.id)
    if mine and mine[0] == user_id:
        accounts.rename(interaction.user.id, current)
    place = int((user_rank or {}).get('serverRank') or 0)
    lines = [f"### {current}\n**{mmr:,}** RP" + (f" | {place:,}위" if place else "")]
    board = [int(r['mmr']) for r in ranked]
    tiers = tier_cuts(season_id, ranked, rank_history.cut_history(season_id), end,
                      cut_rp(rank_300), cut_rp(rank_1000), datetime.now(timezone.utc))
    for tier, icon, _, _, _, _, cut, outlook in tiers:
        if not cut:
            continue
        gap = cut - mmr
        text = f"{app_emojis.tier(icon)} {tier} " + (f"컷까지 **{gap:,}** RP" if gap > 0 else f"컷보다 **{-gap:,}** RP 위")
        # 순위표가 1000위까지라 그 아래 RP는 사이 인원을 셀 수 없음
        if board and min(mmr, cut) >= min(board):
            text += f", 사이에 {sum(1 for v in board if min(mmr, cut) < v < max(mmr, cut)):,}명"
        if outlook:
            final = outlook['future'][-1][1]
            text += f"\n-# 종료 예상 컷 {round(final, -1):,.0f}" + (f"까지 {round(final - mmr):,} RP" if final > mmr else f"보다 {round(mmr - final):,} RP 위")
        lines.append(text.strip())
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

    # 휴면 강등 컷용. 상위 1000명 중 활동을 모르는 계정의 마지막 랭크 게임을 1분에 50명씩 확인
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
                uid, at = await last_ranked_game(self.client, name, season[0], p[rank_history.UID])
            except Exception as e:
                logger.debug(f"마지막 랭크 게임 확인 실패 {name}: {type(e).__name__}")
                uid, at = None, None
            p[rank_history.CHECKED] = now
            if uid:
                p[rank_history.UID] = uid
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
    @handle_errors(user_message="이터컷을 가져오는 중 오류가 발생했습니다.\n잠시 후 다시 시도해주세요.")
    async def rating_command(self, interaction: discord.Interaction):
        """현재 시즌의 이터니티/데미갓 컷을 확인합니다."""
        async def build():
            season = await get_ranked_season()
            if not season:
                raise ServiceDownError("현재 시즌 조회 실패", "현재 시즌 정보를 가져올 수 없습니다.\n잠시 후 다시 시도해주세요.")
            season_id, season_name = season

            (rank_300, rank_1000), season_info, top = await asyncio.gather(
                fetch_rating_info(self.client, season_id), get_season_info(), fetch_ranking_data(self.client, season_id))
            if not rank_300 and not rank_1000:
                raise ServiceDownError(f"{season_name} 이터컷 조회 실패",
                                       f"{season_name} 이터컷을 가져올 수 없습니다.\n잠시 후 다시 시도해주세요.")
            return create_rating_layout(rank_300, rank_1000, season_name, season_id,
                                        season_end_for(season_info, season_id), self.client, top)

        view, message = await send_card(interaction, "이터컷 조회 중", build)
        if view:
            view.message = message

async def setup(client: ERClient):
    """명령어를 등록합니다."""
    await client.add_cog(Rating(client))
