import asyncio
import time
from datetime import datetime, timezone

import discord
from discord import ui
from discord.ext import commands
from discord import app_commands
from typing import Dict, List, Optional, Tuple
from client import ERClient
from commands.season import get_ranked_season

from utils.config import config
from utils.layouts import send_card, CooldownLayoutView
from utils.errors import handle_errors, validate_nickname, NotFoundError, ServiceDownError
from commands import account
from utils import accounts, app_emojis, labs_cut, rank_history, visual
from utils.emojis import EMOJIS
from utils.logging_config import get_logger
from utils.rank_helpers import RANKING_SERVER, SERVER_NAMES, fetch_user_rank, fetch_user_stats_solo, rank_server
from utils.tier_system import TierSystem

logger = get_logger('레이팅')

async def fetch_rating_info(client: ERClient, season_id: int) -> Tuple[Optional[Dict], Optional[Dict]]:
    """labs가 정각에 받은 순위표의 300등과 1000등. 순위표가 그 순위까지 차지 않았으면 labs가 GATE를 씀"""
    data = labs_cut.cut(season_id)
    if not data:
        return None, None
    boards = [(data.get('tiers') or {}).get(key, {}).get('board') for key in ('eternity', 'demigod')]
    return tuple({'mmr': b} if b else None for b in boards)


def cut_rp(user: Optional[Dict]) -> Optional[int]:
    """순위 컷 RP. 시즌 초 순위권 점수가 RANKED_GATE보다 낮으면 GATE가 실제 컷"""
    return max(int(user.get('mmr', 0)), TierSystem.RANKED_GATE) if user else None


def _day_ago(history, index: int) -> Optional[int]:
    now = time.time()
    # 봇이 멈춰 기록이 비면 며칠 전 값과 비교하지 않게 28시간까지만
    old = [h for h in history if 20 * 3600 <= now - h[0] <= 28 * 3600 and h[index]]
    return min(old, key=lambda h: abs(now - h[0] - 86400))[index] if old else None


def tier_cuts(data: Optional[dict]) -> List[tuple]:
    """labs cut.json의 티어마다 (이름, 아이콘, 순위, 순위표 컷, 기록 열, 휴면 강등 컷 결과, 표시 컷, 종료 예상)"""
    tiers = []
    for key, tier, icon, place, official in (('eternity', '이터니티', '10', 300, 1), ('demigod', '데미갓', '9', 1000, 2)):
        t = ((data or {}).get('tiers') or {}).get(key) or {}
        found = (t['cut'], t['dropped'], t.get('estimated')) if t.get('dropped') is not None else None
        outlook = {'future': [(datetime.fromtimestamp(f[0], timezone.utc), f[1], f[2], f[3]) for f in t['forecast']]} \
            if t.get('forecast') else None
        tiers.append((tier, icon, t.get('place', place), t.get('board'), official, found, t.get('cut'), outlook))
    return tiers


def season_end_of(data: Optional[dict]) -> Optional[datetime]:
    end = ((data or {}).get('season') or {}).get('end')
    return datetime.fromtimestamp(end, timezone.utc) if end else None


def create_rating_layout(data: dict, season_name: str, season_id: Optional[int] = None,
                         client: Optional[ERClient] = None, top: Optional[List[Dict]] = None) -> ui.LayoutView:
    """labs cut.json으로 이터컷 카드를 만듭니다. top은 labs가 같은 회차에 받은 아시아1 상위 목록"""
    history = rank_history.cut_history(season_id) if season_id else []
    ranked = [r for r in top or [] if r.get('mmr')]
    now = datetime.now(timezone.utc)
    season_end = season_end_of(data)
    tiers = tier_cuts(data)
    view = RatingView(client, season_id, ranked, *(t[6] for t in tiers)) if client and season_id else ui.LayoutView()

    race = labs_cut.race(season_id)

    def race_block(icon) -> str:
        lines = [f"{app_emojis.tier(icon)} 이터니티 {TierSystem.RANKED_GATE:,} 달성 **{race['count']:,}**명".strip()]
        # 도달한 계정은 도달 시각을, 아직 못 닿은 계정은 남은 RP를 붙임
        for i, r in enumerate(race['list'][:5], 1):
            at = datetime.fromtimestamp(r['at'], rank_history.KST) if r.get('at') else None
            if at:
                tail = f" `{at.month}월 {at.day}일 {at:%H:%M}`"
            elif r['rp'] < TierSystem.RANKED_GATE:
                tail = f" `{TierSystem.RANKED_GATE - r['rp']:,} 남음`"
            else:
                tail = ""
            lines.append(f"`{i}` **{discord.utils.escape_markdown(r['nickname'])}** {r['rp']:,} RP{tail}")
        return "\n".join(lines)

    def block(tier, icon, place, rp, official, found, start, outlook) -> str:
        head = f"{app_emojis.tier(icon)} {tier} {place:,}위".strip()
        if not start:
            return f"{head} 정보 없음"
        text = f"{head} {'약 ' if found and found[2] else ''}**{start:,}** RP"
        before = _day_ago(history, official + 2 if found else official)
        if before:
            text += f" `24시간 {start - before:+,}`"
        if not found and start > TierSystem.RANKED_GATE:
            text += "\n-# 휴면 계정을 확인하는 중이라 순위표 값을 표시합니다."
        return text

    sub = [season_name, SERVER_NAMES[RANKING_SERVER]]
    if season_end:
        season_end = season_end.astimezone(rank_history.KST)
        days = (season_end.date() - datetime.now(rank_history.KST).date()).days
        if days >= 0:
            sub.append(f"시즌 종료 D-{days}" if days else "시즌 종료 D-day")
    sub.append(datetime.fromtimestamp(data.get('updatedAt', 0), rank_history.KST).strftime('%H')+':00 기준')
    children = [
        ui.TextDisplay("### 이터컷\n-# " + " | ".join(sub)),
        # 시즌 초 300위가 8,300을 넘기 전에는 이터니티 컷이 늘 8,300이라 그 자리에 도달 순서를 보여 줌
        ui.TextDisplay("\n".join(race_block(t[1]) if race and t[0] == '이터니티' else block(*t) for t in tiers)),
    ]

    chart = _outlook_chart(history, tiers, now) if season_end else None
    if chart:
        children.append(ui.MediaGallery(discord.MediaGalleryItem(visual.attach(view, 'cut_outlook.png', chart))))
    notes = []
    if any(t[5] for t in tiers):
        notes.append("휴면 강등 반영")
    if chart:
        notes.append("막판 급등 반영")
    if labs_cut.stale(data):
        notes.append("자료 갱신이 늦어지고 있습니다")
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
    labs = ui.Button(style=discord.ButtonStyle.link, label="이터컷 예측", emoji=EMOJIS['labs'], url=f"{config.labs_url}/er/cut")
    if isinstance(view, RatingView):
        view.add_item(ui.ActionRow(view.compare_button, view.spread_button, labs))
    else:
        view.add_item(ui.ActionRow(labs))
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
    data = labs_cut.cut(season_id)
    ranked = [r for r in labs_cut.top(season_id) or [] if r.get('mmr')]
    mmr = int(stats.get('mmr', 0))
    current = stats.get('nickname') or (user_rank or {}).get('nickname') or name
    mine = accounts.get(interaction.user.id)
    if mine and mine[0] == user_id:
        accounts.rename(interaction.user.id, current)
    place = int((user_rank or {}).get('serverRank') or 0)
    lines = [f"### {current}\n**{mmr:,}** RP" + (f" | {place:,}위" if place else "")]
    board = [int(r['mmr']) for r in ranked]
    tiers = tier_cuts(data)
    for tier, icon, _, _, _, _, cut, outlook in tiers:
        if not cut:
            continue
        gap = cut - mmr
        text = f"{app_emojis.tier(icon)} {tier} " + (f"컷까지 **{gap:,}** RP" if gap > 0 else f"컷보다 **{-gap:,}** RP 위")
        # 순위표가 1000위까지라 그 아래 RP는 사이 인원을 셀 수 없음
        if board and min(mmr, cut) >= min(board):
            text += f", 사이에 {sum(1 for v in board if min(mmr, cut) < v < max(mmr, cut)):,}명"
        if outlook:
            # 보이는 종료 예상이 10 단위라 차이도 같은 값으로 계산
            final = round(outlook['future'][-1][1], -1)
            text += f"\n-# 종료 예상 컷 {final:,.0f}" + (f"까지 {final - mmr:,.0f} RP" if final > mmr else f"보다 {mmr - final:,.0f} RP 위")
        lines.append(text.strip())
    if rank_server(user_rank) not in (None, RANKING_SERVER):
        lines.append(f"-# 컷은 {SERVER_NAMES[RANKING_SERVER]} 기준")
    view = ui.LayoutView()
    view.add_item(ui.Container(ui.TextDisplay("\n".join(lines)), accent_colour=visual.colour('cut')))
    # labs 진단 칸은 0~20000만 받음
    query = f"?rp={mmr}" if 0 < mmr <= 20000 else ""
    view.add_item(ui.ActionRow(ui.Button(style=discord.ButtonStyle.link, label="내 RP 진단", emoji=EMOJIS['labs'],
                                         url=f"{config.labs_url}/er/cut{query}#check-h")))
    await interaction.followup.send(view=view, ephemeral=True)


class Rating(commands.Cog):
    def __init__(self, client: ERClient):
        self.client = client

    @app_commands.command(name="이터컷", description="이터니티와 데미갓 컷")
    @handle_errors(user_message="이터컷을 가져오는 중 오류가 발생했습니다.\n잠시 후 다시 시도해주세요.")
    async def rating_command(self, interaction: discord.Interaction):
        """현재 시즌의 이터니티/데미갓 컷을 확인합니다."""
        async def build():
            season = await get_ranked_season()
            if not season:
                raise ServiceDownError("현재 시즌 조회 실패", "현재 시즌 정보를 가져올 수 없습니다.\n잠시 후 다시 시도해주세요.")
            season_id, season_name = season

            data = labs_cut.cut(season_id)
            if not data:
                raise ServiceDownError(f"{season_name} 이터컷 자료 없음",
                                       f"{season_name} 이터컷을 가져올 수 없습니다.\n잠시 후 다시 시도해주세요.")
            return create_rating_layout(data, season_name, season_id, self.client, labs_cut.top(season_id))

        view, message = await send_card(interaction, "이터컷 조회 중", build)
        if view:
            view.message = message

async def setup(client: ERClient):
    """명령어를 등록합니다."""
    await client.add_cog(Rating(client))
