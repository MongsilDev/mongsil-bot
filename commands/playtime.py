from urllib.parse import quote

import discord
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, NamedTuple, Tuple
from client import ERClient
from discord import app_commands, ui
from discord.ext import commands
from utils.config import config
from utils.layouts import create_loading_layout
from utils.errors import handle_errors, validate_nickname, NotFoundError
from utils.logging_config import get_logger
from utils.emojis import EMOJIS
from utils import visual

logger = get_logger('플탐')

KST = ZoneInfo('Asia/Seoul')

class GameStats(NamedTuple):
    """게임 통계 정보를 저장하는 네임드 튜플"""
    date: datetime.date
    play_time: int
    mode: int
    nickname: str
    start: datetime


class PlayTimeStats(NamedTuple):
    """플레이 타임 통계 정보를 저장하는 네임드 튜플"""
    total_seconds: int
    daily_stats: Dict[datetime.date, int]
    games_played: int
    mode_counts: Dict[str, int]
    nickname: str
    hourly: List[List[int]]
    previous_seconds: Optional[int]
    longest: Optional[Tuple[datetime, int]]

# 요일 상수
WEEKDAYS = {
    0: '월',
    1: '화',
    2: '수',
    3: '목',
    4: '금',
    5: '토',
    6: '일'
}

MODE_NAMES = {3: '랭크', 2: '일반', 6: '코발트', 9: '론울프'}

# 게임 사이 대기가 이보다 짧으면 한 번에 이어서 한 것으로 봄
SESSION_GAP = timedelta(minutes=20)


async def get_user_games(client, user_id: str, start_date: datetime.date) -> Tuple[List[GameStats], bool]:
    """start_date 이후 게임 기록과 그 날짜까지 다 읽었는지 여부"""
    games = []
    next_cursor = None
    # 페이지당 10게임
    max_requests = 30
    request_count = 0

    try:
        while request_count < max_requests:
            url = f"{config.api_url}/user/games/uid/{user_id}"
            if next_cursor:
                url += f"?next={next_cursor}"

            data = await client.api_client.get(url, use_cache=True, ttl=300)
            if not data:
                logger.error("게임 기록 조회 실패")
                return games, False
            for game in data.get('userGames', data.get('games', [])):
                try:
                    started = datetime.strptime(game['startDtm'], "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(KST)
                except (ValueError, KeyError):
                    continue
                if started.date() < start_date:
                    return games, True
                games.append(GameStats(
                    date=started.date(),
                    play_time=game.get('playTime', 0),
                    mode=game.get('matchingMode', 0),
                    nickname=game.get('nickname', ''),
                    start=started,
                ))

            if not data.get('next'):
                return games, True
            next_cursor = data['next']
            request_count += 1
        return games, False
    except Exception as e:
        logger.error(f"게임 기록 조회 중 오류: {e}", exc_info=True)
        raise


def _hourly(games: List[GameStats], dates: List[datetime.date]) -> List[List[int]]:
    """날짜별 24시간 칸에 플레이 초를 시간대에 맞게 나눠 담음"""
    rows = {date: [0] * 24 for date in dates}
    for game in games:
        t, left = game.start, game.play_time
        while left > 0:
            chunk = min(left, 3600 - t.minute * 60 - t.second)
            if t.date() in rows:
                rows[t.date()][t.hour] += chunk
            t += timedelta(seconds=chunk)
            left -= chunk
    return [rows[date] for date in sorted(dates)]


def _longest_session(games: List[GameStats]) -> Optional[Tuple[datetime, int]]:
    best = None
    first = end = None
    for game in sorted(games, key=lambda g: g.start):
        if first is None or game.start - end > SESSION_GAP:
            first = game.start
        end = max(end or game.start, game.start + timedelta(seconds=game.play_time))
        length = int((end - first).total_seconds())
        if best is None or length > best[1]:
            best = (first, length)
    return best


def calculate_play_time_stats(games: List[GameStats], dates: List[datetime.date], nickname: str,
                              previous_seconds: Optional[int] = None) -> PlayTimeStats:
    """플레이 타임 통계를 계산합니다."""
    games = [g for g in games if g.date >= min(dates)]
    daily_stats = {date: 0 for date in dates}
    mode_counts = {name: 0 for name in MODE_NAMES.values()}
    mode_counts['기타'] = 0

    for game in games:
        if game.date in daily_stats:
            daily_stats[game.date] += game.play_time
        mode_counts[MODE_NAMES.get(game.mode, '기타')] += 1

    return PlayTimeStats(
        total_seconds=sum(daily_stats.values()),
        daily_stats=daily_stats,
        games_played=len(games),
        mode_counts={name: count for name, count in mode_counts.items() if count},
        nickname=(games[0].nickname if games else '') or nickname,
        hourly=_hourly(games, dates),
        previous_seconds=previous_seconds,
        longest=_longest_session(games),
    )

def format_duration(seconds: int) -> str:
    """초 단위 시간을 사용자 친화적인 형식으로 변환합니다."""
    if seconds < 60:
        return f"{seconds}초"
    elif seconds < 3600:
        minutes = seconds // 60
        return f"{minutes}분"
    else:
        hours, remainder = divmod(seconds, 3600)
        minutes = remainder // 60
        if minutes > 0:
            return f"{hours}시간 {minutes}분"
        else:
            return f"{hours}시간"


def _short(seconds: int) -> str:
    if seconds == 0:
        return '-'
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"{hours}시간 {minutes}분" if hours else f"{minutes}분"


def _peak_hours(hourly: List[List[int]], width: int = 3) -> Optional[Tuple[int, int]]:
    by_hour = [sum(row[h] for row in hourly) for h in range(24)]
    if not any(by_hour):
        return None
    start = max(range(24), key=lambda h: sum(by_hour[(h + i) % 24] for i in range(width)))
    return start, (start + width) % 24 or 24


def create_playtime_layout(stats: PlayTimeStats) -> ui.LayoutView:
    """플레이 타임 LayoutView를 생성합니다."""
    view = ui.LayoutView()
    dates = sorted(stats.daily_stats)

    sub = [f"{stats.games_played}게임"]
    if stats.previous_seconds is not None:
        diff = stats.total_seconds - stats.previous_seconds
        sign = '+' if diff >= 0 else '-'
        sub.insert(0, f"지난 7일보다 **{sign}{format_duration(abs(diff)) if diff else '0분'}**")
    children = [
        ui.TextDisplay(f"### {stats.nickname}\n-# 최근 7일 플레이 타임"),
        ui.TextDisplay(f"# {format_duration(stats.total_seconds)}\n-# " + " | ".join(sub)),
    ]

    labels = [f"{d.month}/{d.day} {WEEKDAYS[d.weekday()]}" for d in dates]
    totals = [_short(stats.daily_stats[d]) for d in dates]
    chart = visual.heatmap(stats.hourly, labels, visual.COLOURS['playtime'], totals)
    url = visual.attach(view, 'playtime.png', chart)
    children.append(ui.MediaGallery(discord.MediaGalleryItem(url)))

    lines = [f"하루 평균 **{format_duration(stats.total_seconds // 7)}**"]
    if stats.games_played:
        lines[0] += f" | 게임당 **{format_duration(stats.total_seconds // stats.games_played)}**"
    peak = _peak_hours(stats.hourly)
    if peak:
        lines.append(f"주 플레이 시간대 **{peak[0]}시~{peak[1]}시**")
    if stats.longest and stats.longest[1] >= 1800:
        start, length = stats.longest
        lines.append(f"최장 연속 **{format_duration(length)}** {start.month}/{start.day} {WEEKDAYS[start.weekday()]}")
    lines.append("-# " + " | ".join(f"{name} {count}게임" for name, count in stats.mode_counts.items()))
    children.append(ui.TextDisplay("\n".join(lines)))

    view.add_item(ui.Container(*children, accent_colour=visual.colour('playtime')))
    view.add_item(dakgg_row(stats.nickname))
    return view


def dakgg_row(nickname: str) -> ui.ActionRow:
    return ui.ActionRow(
        ui.Button(style=discord.ButtonStyle.link, label="DAK.GG", emoji=EMOJIS['chart'],
                  url=f"https://dak.gg/er/players/{quote(nickname)}")
    )


async def get_playtime_info(client: ERClient, nickname: str) -> Optional[PlayTimeStats]:
    """플레이어의 플레이 타임 정보를 가져옵니다.

    없는 닉네임은 NotFoundError, 유저는 있는데 기록이 없으면 None.
    API 오류는 전파해 handle_errors가 안내한다.
    """
    # 유저 UID 조회
    user_id = await client.get_user_nickname(nickname)
    if not user_id:
        raise NotFoundError(
            f"유저를 찾을 수 없습니다: {nickname}",
            f"'{nickname}' 유저를 찾을 수 없습니다.\n닉네임을 다시 확인해주세요."
        )

    # 게임 날짜(startDtm)가 KST라 버킷도 KST 기준이어야 자정~오전 게임이 누락되지 않는다
    today = datetime.now(KST).date()
    dates = [(today - timedelta(days=i)) for i in range(7)]
    previous_start = today - timedelta(days=13)

    # 지난 7일 비교용으로 14일치를 읽되, 페이지 한도에 걸리면 비교는 생략
    games, complete = await get_user_games(client, user_id, previous_start)
    previous = [g for g in games if g.date < dates[-1]]
    if not any(g.date >= dates[-1] for g in games):
        return None

    return calculate_play_time_stats(
        games, dates, nickname, sum(g.play_time for g in previous) if complete else None)


def no_playtime_layout(nickname: str) -> ui.LayoutView:
    view = ui.LayoutView()
    view.add_item(ui.Container(
        ui.TextDisplay(f"### {nickname}\n최근 7일 플레이 기록이 없습니다."),
        accent_colour=visual.colour('playtime'),
    ))
    view.add_item(dakgg_row(nickname))
    return view


async def build_playtime_view(client: ERClient, nickname: str) -> ui.LayoutView:
    """명령과 /랭크의 플탐 버튼이 같이 씀"""
    stats = await get_playtime_info(client, nickname)
    return create_playtime_layout(stats) if stats else no_playtime_layout(nickname)

class Playtime(commands.Cog):
    def __init__(self, client: ERClient):
        self.client = client

    @app_commands.command(name="플탐", description="최근 7일 플레이 타임")
    @app_commands.describe(닉네임="이터널 리턴 닉네임")
    @handle_errors(user_message="플레이 타임 정보를 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
    async def playtime(
        self,
        interaction: discord.Interaction,
        닉네임: str
    ):
        """플레이어의 최근 7일 플레이 타임을 조회합니다."""
        # 닉네임 검증
        validated_nickname = validate_nickname(닉네임)

        await interaction.response.send_message(view=create_loading_layout("플레이 타임 조회 중"))

        view = await build_playtime_view(self.client, validated_nickname)
        await interaction.edit_original_response(view=view, attachments=visual.files_of(view))

async def setup(client: ERClient):
    """명령어를 등록합니다."""
    await client.add_cog(Playtime(client))
