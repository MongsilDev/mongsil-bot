from urllib.parse import quote

import discord
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Dict, List, Optional, NamedTuple
from client import ERClient
from discord import app_commands, ui
from discord.ext import commands
from utils.config import config
from utils.layouts import create_loading_layout
from utils.errors import handle_errors, validate_nickname, NotFoundError
from utils.logging_config import get_logger
from utils.emojis import EMOJIS

logger = get_logger('플탐')

KST = ZoneInfo('Asia/Seoul')

class GameStats(NamedTuple):
    """게임 통계 정보를 저장하는 네임드 튜플"""
    date: datetime.date
    play_time: int
    mode: int
    nickname: str

class PlayTimeStats(NamedTuple):
    """플레이 타임 통계 정보를 저장하는 네임드 튜플"""
    total_seconds: int
    daily_stats: Dict[datetime.date, int]
    games_played: int
    mode_counts: Dict[str, int]
    nickname: str

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

async def get_user_games(client, user_id: str, start_date: datetime.date) -> List[GameStats]:
    """유저의 게임 기록을 가져옵니다."""
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
            if data:
                current_games = data.get('userGames', data.get('games', []))

                # 날짜 체크 및 게임 추가
                for game in current_games:
                    try:
                        game_date = datetime.strptime(
                            game['startDtm'],
                            "%Y-%m-%dT%H:%M:%S.%f%z"
                        ).date()

                        if game_date < start_date:
                            return games

                        games.append(GameStats(
                            date=game_date,
                            play_time=game.get('playTime', 0),
                            mode=game.get('matchingMode', 0),
                            nickname=game.get('nickname', ''),
                        ))
                    except (ValueError, KeyError):
                        continue

                if 'next' not in data or not data['next']:
                    break
                next_cursor = data['next']
                request_count += 1
            else:
                logger.error("게임 기록 조회 실패")
                break

        return games
    except Exception as e:
        logger.error(f"게임 기록 조회 중 오류: {e}", exc_info=True)
        raise

def calculate_play_time_stats(games: List[GameStats], dates: List[datetime.date], nickname: str) -> PlayTimeStats:
    """플레이 타임 통계를 계산합니다."""
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
        nickname=games[0].nickname or nickname,
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

def create_playtime_layout(stats: PlayTimeStats) -> ui.LayoutView:
    """플레이 타임 LayoutView를 생성합니다."""
    daily_avg = stats.total_seconds // 7
    daily_chart = create_daily_chart(stats.daily_stats)

    view = ui.LayoutView()
    children = []

    # Header
    children.append(ui.TextDisplay(f"### {stats.nickname}\n-# 최근 7일 플레이 타임"))
    children.append(ui.Separator())

    # Summary stats - two clean lines
    summary = f"**{stats.games_played}**게임 | 총 **{format_duration(stats.total_seconds)}**"
    summary += f"\n일일 평균 **{format_duration(daily_avg)}**"
    if stats.games_played > 0:
        avg_game = stats.total_seconds // stats.games_played
        summary += f" | 게임당 평균 **{format_duration(avg_game)}**"
    summary += "\n-# " + " | ".join(f"{name} {count}게임" for name, count in stats.mode_counts.items())
    children.append(ui.TextDisplay(summary))
    children.append(ui.Separator())

    # Daily chart
    children.append(ui.TextDisplay(daily_chart))

    view.add_item(ui.Container(*children, accent_colour=discord.Colour.blurple()))

    view.add_item(dakgg_row(stats.nickname))
    return view


def dakgg_row(nickname: str) -> ui.ActionRow:
    return ui.ActionRow(
        ui.Button(style=discord.ButtonStyle.link, label="DAK.GG", emoji=EMOJIS['chart'],
                  url=f"https://dak.gg/er/players/{quote(nickname)}")
    )

def create_daily_chart(daily_stats: Dict[datetime.date, int]) -> str:
    """일일 플레이 타임을 시각적 차트로 표현합니다."""
    chart_lines = []
    max_time = max(daily_stats.values()) if daily_stats else 1

    for date, play_time in sorted(daily_stats.items()):
        weekday = WEEKDAYS[date.weekday()]
        date_str = f"{f'{date.month}/{date.day}':>5} {weekday}"

        if play_time == 0:
            chart_lines.append(f"`{date_str}` ▱▱▱▱▱▱▱▱▱▱ `-`")
        else:
            # 진행률 바 생성 (10칸)
            progress = max(1, min(10, round((play_time / max_time) * 10)))
            bar = "▰" * progress + "▱" * (10 - progress)
            time_str = format_duration(play_time)
            chart_lines.append(f"`{date_str}` {bar} `{time_str}`")

    return "\n".join(chart_lines)

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

    # 오늘을 포함한 최근 7일 날짜 리스트 생성 (오늘부터 6일 전까지)
    # 게임 날짜(startDtm)가 KST라 버킷도 KST 기준이어야 자정~오전 게임이 누락되지 않는다
    today = datetime.now(KST).date()
    dates = [(today - timedelta(days=i)) for i in range(7)]  # 오늘부터 6일 전까지
    start_date = dates[-1]  # 가장 오래된 날짜

    # 게임 기록 조회
    games = await get_user_games(client, user_id, start_date)
    if not games:
        return None

    return calculate_play_time_stats(games, dates, nickname)

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

        # 플레이 타임 정보 조회
        stats = await get_playtime_info(self.client, validated_nickname)
        if not stats:
            no_data_view = ui.LayoutView()
            no_data_view.add_item(ui.Container(
                ui.TextDisplay(f"### {validated_nickname}\n최근 7일 플레이 기록이 없습니다."),
                accent_colour=discord.Colour.blurple(),
            ))
            no_data_view.add_item(dakgg_row(validated_nickname))
            await interaction.edit_original_response(view=no_data_view)
            return

        view = create_playtime_layout(stats)
        await interaction.edit_original_response(view=view)

async def setup(client: ERClient):
    """명령어를 등록합니다."""
    await client.add_cog(Playtime(client))
