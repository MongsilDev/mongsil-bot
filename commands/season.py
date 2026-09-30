import asyncio
import re

import discord
from discord import ui
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timedelta
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo
from client import ERClient

from utils.config import config
from utils.layouts import create_error_layout
from utils.errors import handle_errors
from utils.logging_config import get_logger
from utils.emojis import EMOJIS
from utils import visual

logger = get_logger('시즌')

# 한국 시간대 설정
KST = ZoneInfo('Asia/Seoul')

# 시즌 관련 상수
SEASON_ZERO_ID = 19  # 시즌 0이 되는 ID 값
SEASON_NAME_OFFSET = 9  # Season16은 시즌7이므로, 9를 빼면 됨

# 시즌 코드명 (API 미제공 → seasonID 기준 수동 매핑, 새 시즌마다 한 줄 추가)
SEASON_CODENAMES: Dict[int, str] = {
    39: "쁘띠 미뇽",  # 정규 시즌 11
    41: "세일링",  # 정규 시즌 12
}

# 시즌 데이터 캐시 (API 호출 최소화)
_season_cache: Optional[Dict[str, Any]] = None
_season_cache_time: Optional[datetime] = None
SEASON_CACHE_TTL = timedelta(minutes=30)

def get_season_name(season_id: int, season_name: str) -> str:
    """
    시즌 ID와 시즌 이름을 기반으로 한국어 시즌 이름을 생성합니다.

    Args:
        season_id: 시즌 ID
        season_name: API에서 받은 시즌 이름 (예: "Season16", "Pre-Season7")

    Returns:
        한국어로 번역된 시즌 이름
    """
    if season_id <= SEASON_ZERO_ID:
        return f"EA 시즌 {season_id}"

    try:
        # seasonName에서 시즌 번호 추출
        if season_name.startswith("Pre-Season"):
            season_number = int(season_name.replace("Pre-Season", ""))
            actual_season = season_number - SEASON_NAME_OFFSET
            return f"프리 시즌 {actual_season}"
        elif season_name.startswith("Season"):
            season_number = int(season_name.replace("Season", ""))
            actual_season = season_number - SEASON_NAME_OFFSET
            return f"정규 시즌 {actual_season}"
        else:
            return season_name
    except ValueError as e:
        logger.error(f"시즌 이름 파싱 오류: {e}")
        return season_name

class SeasonInfo(NamedTuple):
    """시즌 정보를 저장하는 네임드 튜플"""
    number: int
    start_date: datetime
    end_date: datetime
    name: str

async def fetch_season_data(season_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    API에서 시즌 정보를 가져옵니다. 캐시를 사용하여 API 호출을 최소화합니다.

    Args:
        season_id: 특정 시즌 ID를 찾을 경우 사용. None이면 현재 시즌(isCurrent=1)을 반환.

    Returns:
        시즌 정보 딕셔너리 또는 None
    """
    global _season_cache, _season_cache_time

    # 현재 시즌 조회 시 캐시 확인
    if season_id is None and _season_cache is not None and _season_cache_time is not None:
        if datetime.now() - _season_cache_time < SEASON_CACHE_TTL:
            return _season_cache

    try:
        from utils.api_client import api_client

        # API URL을 config에서 가져오되, v2 버전 사용
        base_url = config.api_url.replace('/v1', '/v2')
        url = f'{base_url}/data/Season'

        # 모듈 캐시(30분)와 정렬. api_client 기본 TTL(1시간)이 더 길면 30분 갱신이 무력화된다
        data = await api_client.get(url, use_cache=True, ttl=1800)

        if not data:
            logger.error("시즌 API 응답 없음")
            return None

        # API 응답 형식 유연하게 처리 (data 필드가 리스트인 경우와 아닌 경우)
        season_list = data.get('data', [])
        if not isinstance(season_list, list):
            season_list = [season_list] if season_list else []

        if not season_list:
            logger.error("시즌 데이터가 비어있습니다")
            return None

        # 특정 season_id를 찾는 경우
        if season_id is not None:
            for season in season_list:
                if isinstance(season, dict) and season.get('seasonID') == season_id:
                    return season
            return None

        # 현재 시즌 찾기 (isCurrent = 1)
        current_season = None
        for season in season_list:
            if isinstance(season, dict) and season.get('isCurrent') == 1:
                current_season = season
                break

        # isCurrent가 없는 경우 가장 높은 seasonID 사용
        if not current_season and season_list:
            current_season = max(
                (s for s in season_list if isinstance(s, dict) and 'seasonID' in s),
                key=lambda s: s['seasonID'],
                default=None
            )

        if current_season:
            # 캐시 업데이트
            _season_cache = current_season
            _season_cache_time = datetime.now()

        return current_season

    except Exception as e:
        logger.error(f"시즌 API 호출 중 오류: {e}", exc_info=True)
        return None

async def get_ranked_season() -> Optional[Tuple[int, str]]:
    """
    랭크 조회에 쓸 시즌 ID와 한국어 시즌 이름을 가져옵니다.

    프리시즌에는 랭킹/랭크 통계 데이터가 없으므로 직전 정규 시즌으로 대체합니다.

    Returns:
        (시즌 ID, 시즌 이름) 튜플 또는 None
    """
    season_data = await fetch_season_data()
    if not season_data or 'seasonID' not in season_data:
        return None

    season_id = season_data['seasonID']
    season_name_raw = season_data.get('seasonName', '')

    if season_name_raw.startswith('Pre-Season'):
        for prev_id in range(season_id - 1, SEASON_ZERO_ID, -1):
            prev = await fetch_season_data(prev_id)
            if prev and not prev.get('seasonName', '').startswith('Pre-Season'):
                season_id = prev['seasonID']
                season_name_raw = prev.get('seasonName', '')
                break

    return season_id, get_season_name(season_id, season_name_raw)

def _parse_season_date(date_str: str) -> Optional[datetime]:
    """시즌 날짜 문자열을 파싱합니다. 여러 형식을 지원합니다."""
    formats = [
        '%Y-%m-%dT%H:%M:%S%z',      # ISO 8601 with timezone
        '%Y-%m-%dT%H:%M:%S.%f%z',   # ISO 8601 with microseconds
        '%Y-%m-%dT%H:%M:%S',         # ISO 8601 without timezone
        '%Y-%m-%d %H:%M:%S',         # 환경변수 형식
        '%Y/%m/%d %H:%M:%S',         # 슬래시 형식
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=KST)
            return dt
        except ValueError:
            continue
    return None

async def get_season_info() -> Optional[SeasonInfo]:
    """현재 시즌 정보를 가져옵니다. 실패하면 None."""
    season_data = await fetch_season_data()
    if not season_data:
        return None

    season_id = season_data.get('seasonID')
    start_date = _parse_season_date(season_data.get('seasonStart', ''))
    end_date = _parse_season_date(season_data.get('seasonEnd', ''))
    if not (season_id and start_date and end_date):
        logger.error(f"시즌 데이터 형식 이상: {season_data}")
        return None

    return SeasonInfo(
        number=season_id,
        start_date=start_date,
        end_date=end_date,
        name=get_season_name(season_id, season_data.get('seasonName', '')),
    )

PATCH_NOTES_API = 'https://playeternalreturn.com/api/v1/posts/news?category=patchnote&page=1&hl=ko-KR'
PATCH_NOTES_URL = "https://playeternalreturn.com/posts/news?categoryPath=patchnote&hl=ko-KR"


class PatchNote(NamedTuple):
    version: str
    applied: datetime
    url: str


PATCH_TITLE = re.compile(r'(\d{4})\.(\d{2})\.(\d{2})\s*-\s*(\d+\.\d+)\s*패치')


async def fetch_patch_notes() -> List[PatchNote]:
    """공식 사이트 정기 패치 노트, 적용일 순. 핫픽스 제외. 실패하면 빈 목록"""
    try:
        from utils.api_client import api_client
        data = await api_client.get(PATCH_NOTES_API, ttl=1800)
        notes = []
        for article in data['articles']:
            text = article['i18ns'].get('ko_KR') or next(iter(article['i18ns'].values()))
            m = PATCH_TITLE.search(text['title'])
            if m:
                applied = datetime(int(m[1]), int(m[2]), int(m[3]), 11, tzinfo=KST)
                notes.append(PatchNote(m[4], applied, article.get('url') or PATCH_NOTES_URL))
        return sorted(notes, key=lambda n: n.applied)
    except Exception as e:
        logger.warning(f"패치 노트 조회 실패: {type(e).__name__}: {e}")
        return []


def expected_patches(known: Sequence[PatchNote], end: datetime) -> List[PatchNote]:
    """정기 패치는 목요일 2주 간격. 마지막 공지 이후 시즌 종료 전까지 예상일, 버전은 소수점 뒤 1씩 증가"""
    if not known:
        return []
    last = known[-1]
    major, minor = last.version.split('.')
    out = []
    when, n = last.applied + timedelta(days=14), int(minor) + 1
    while when.date() < end.date():
        out.append(PatchNote(f"{major}.{n}", when, PATCH_NOTES_URL))
        when, n = when + timedelta(days=14), n + 1
    return out


def create_season_layout(season_info: Optional[SeasonInfo], patches: Sequence[PatchNote] = ()) -> ui.LayoutView:
    """시즌 정보 LayoutView를 생성합니다."""
    if not season_info:
        return create_error_layout("현재 시즌 정보를 가져올 수 없습니다. 잠시 후 다시 시도해주세요.")

    now = datetime.now(KST)
    start, end = season_info.start_date, season_info.end_date
    codename = SEASON_CODENAMES.get(season_info.number)
    title = f"{season_info.name} | {codename}" if codename else season_info.name

    view = ui.LayoutView(timeout=None)
    in_season = [p for p in patches if start.date() <= p.applied.date() <= end.date()]
    guesses = expected_patches(in_season, end)
    chart = visual.season_timeline(start, end, now, [(p.applied, p.version) for p in in_season],
                                   visual.COLOURS['season'], [(p.applied, p.version) for p in guesses])
    view.add_item(ui.Container(ui.TextDisplay(f"### {title}"),
                               ui.MediaGallery(discord.MediaGalleryItem(visual.attach(view, 'season.png', chart))),
                               accent_colour=visual.colour('season')))

    current = [p for p in patches if p.applied <= now]
    upcoming = [p for p in patches if p.applied > now]
    latest = (upcoming or current or [None])[0 if upcoming else -1]
    view.add_item(ui.ActionRow(
        ui.Button(style=discord.ButtonStyle.link, label="공식 사이트", url="https://playeternalreturn.com/", emoji=EMOJIS['web']),
        ui.Button(style=discord.ButtonStyle.link, label=f"{latest.version} 패치 노트" if latest else "패치 노트",
                  url=latest.url if latest else PATCH_NOTES_URL, emoji=EMOJIS['patch_note']),
    ))
    return view


class Season(commands.Cog):
    """시즌 관련 명령어를 처리하는 Cog"""

    def __init__(self, client: ERClient):
        self.client = client

    @app_commands.command(name="시즌", description="현재 시즌 기간")
    @handle_errors(user_message="시즌 정보를 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
    async def season_command(self, interaction: discord.Interaction):
        """
        현재 시즌 정보를 보여줍니다.

        시즌 이름, 시작일, 종료일, 진행도를 포함한 정보를 표시합니다.
        상호작용 버튼을 통해 공식 사이트와 패치 노트에 접근할 수 있습니다.
        """
        await interaction.response.defer()

        season_info, patches = await asyncio.gather(get_season_info(), fetch_patch_notes())
        layout = create_season_layout(season_info, patches)

        await interaction.followup.send(view=layout, files=visual.files_of(layout))

async def setup(client: ERClient):
    """명령어를 등록합니다."""
    await client.add_cog(Season(client))
