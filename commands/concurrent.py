import os
import pickle
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

import discord
from discord import ui
from discord import app_commands
from discord.ext import commands, tasks

from client import ERClient
from utils.api_client import api_client
from utils.config import config
from utils.layouts import create_error_layout
from utils.errors import APIError, handle_errors
from utils.logging_config import get_logger
from utils.emojis import EMOJIS

logger = get_logger('동접')

# Steam API URL
STEAM_API_URL = 'https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/'

FAIL_ALERT_THRESHOLD = 5


def _as_utc(t: datetime) -> datetime:
    """naive 시각은 과거 저장분(서버 로컬=UTC)으로 간주한다."""
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

class ConcurrentData:
    """동시접속자 데이터 관리 클래스"""

    def __init__(self, data_dir: str = 'data'):
        # 24시간 * 60분 = 1440개 최대
        self.data = deque(maxlen=1440)
        self.data_dir = data_dir
        self.file_path = os.path.join(data_dir, 'concurrent_data.pkl')

        # 데이터 디렉토리 생성
        os.makedirs(data_dir, exist_ok=True)

    def add_data(self, time: datetime, count: int):
        """새로운 동시접속자 데이터를 추가합니다."""
        # 24시간이 지난 데이터는 deque가 자동으로 제거
        self.data.append((time, count))

    def get_statistics(self):
        """최근 24시간 범위의 통계를 계산합니다.

        deque는 개수(1440) 기준이라 수집 공백이 있으면 24시간 밖 데이터가
        남아 있을 수 있어 시간으로 한 번 더 거른다.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recent = [(t, c) for t, c in self.data if _as_utc(t) >= cutoff]

        if not recent:
            return {
                'max_count': 0,
                'max_time': None,
                'min_count': 0,
                'min_time': None,
                'data_count': 0
            }

        max_data = max(recent, key=lambda x: (x[1], _as_utc(x[0])))
        min_data = min(recent, key=lambda x: (x[1], _as_utc(x[0])))

        return {
            'max_count': max_data[1],
            'max_time': _as_utc(max_data[0]),
            'min_count': min_data[1],
            'min_time': _as_utc(min_data[0]),
            'data_count': len(recent)
        }

    def save_to_file(self):
        """pickle로 데이터를 저장합니다."""
        try:
            # 임시 파일로 먼저 저장한 후 원자적 이동
            temp_file_path = f"{self.file_path}.tmp"

            with open(temp_file_path, 'wb') as f:
                pickle.dump(self, f, protocol=pickle.HIGHEST_PROTOCOL)

            # 원자적 파일 이동 (os.replace는 대상 파일 존재 여부와 무관하게 동작)
            os.replace(temp_file_path, self.file_path)

            return True

        except (OSError, IOError) as e:
            logger.error(f"동접 데이터 저장 중 I/O 오류: {e}")
            # 임시 파일 정리
            if os.path.exists(temp_file_path):
                try:
                    os.remove(temp_file_path)
                except OSError:
                    pass
            return False
        except Exception as e:
            logger.error(f"동접 데이터 저장 중 예상치 못한 오류: {e}")
            return False

    @classmethod
    def load_from_file(cls, data_dir: str = 'data'):
        """pickle에서 데이터를 로드합니다."""
        file_path = os.path.join(data_dir, 'concurrent_data.pkl')

        if os.path.exists(file_path):
            try:
                with open(file_path, 'rb') as f:
                    data = pickle.load(f)
                    return data
            except (pickle.PickleError, OSError, EOFError) as e:
                logger.warning(f"파일 로드 실패: {e}")
            except Exception as e:
                logger.error(f"예상치 못한 파일 로드 오류: {e}")

        # 파일이 없거나 로드 실패 시 새 인스턴스 생성
        return cls(data_dir)

concurrent_data = ConcurrentData.load_from_file()

async def get_current_player_count() -> Optional[int]:
    """Steam API를 통해 현재 플레이어 수를 가져옵니다."""
    try:
        if not config.steam_api_key:
            logger.warning("STEAM_API_KEY가 설정되지 않았습니다.")
            return None

        params = {
            'appid': config.appid_erbs,
            'key': config.steam_api_key
        }

        data = await api_client.get(STEAM_API_URL, params=params, use_cache=False)

        if not data:
            logger.warning("Steam API 응답이 비어있습니다.")
            return None

        # API 응답 검증
        if 'response' not in data:
            logger.warning("Steam API 응답에 'response' 필드가 없습니다.")
            return None

        response = data['response']

        # result 필드 검증 (1이 성공을 의미)
        if response.get('result') != 1:
            logger.warning(f"Steam API 오류: result={response.get('result')}")
            return None

        if 'player_count' not in response:
            logger.warning("Steam API 응답에 'player_count' 필드가 없습니다.")
            return None

        player_count = response['player_count']
        if not isinstance(player_count, int) or player_count < 0:
            logger.warning(f"잘못된 플레이어 수 값: {player_count}")
            return None

        return player_count

    except APIError as e:
        logger.warning(f"플레이어 수 조회 실패: {e.message}")
        return None
    except Exception as e:
        logger.error(f"플레이어 수 조회 중 오류 발생: {e}", exc_info=True)
        return None

def create_concurrent_layout(current_count: int) -> ui.LayoutView:
    """동시 접속자 수 LayoutView를 생성합니다."""
    now_ts = int(datetime.now(timezone.utc).timestamp())
    stats = concurrent_data.get_statistics()

    children = [
        ui.TextDisplay("### 이터널 리턴 동시 접속자"),
        ui.Separator(),
        ui.TextDisplay(f"## {current_count:,}명\n-# <t:{now_ts}:t> 기준"),
    ]

    if stats['data_count'] > 0 and stats['max_time']:
        max_ts = int(stats['max_time'].timestamp())
        min_ts = int(stats['min_time'].timestamp())
        children.append(ui.Separator())
        children.append(ui.TextDisplay(
            f"24시간 최고 **{stats['max_count']:,}**명 <t:{max_ts}:t>\n"
            f"24시간 최저 **{stats['min_count']:,}**명 <t:{min_ts}:t>"
        ))

    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(*children, accent_colour=discord.Colour.blurple()))

    view.add_item(ui.ActionRow(
        ui.Button(style=discord.ButtonStyle.link, label="SteamDB",
                  emoji=EMOJIS['chart'], url="https://steamdb.info/app/1049590/graphs/")
    ))
    return view

class Concurrent(commands.Cog):
    def __init__(self, client: ERClient):
        self.client = client
        self._fail_streak = 0
        self.save_concurrent_data.start()

    def cog_unload(self):
        """Cog 언로드 시 태스크 정지 및 데이터 저장"""
        if self.save_concurrent_data.is_running():
            self.save_concurrent_data.cancel()
        # 봇 종료 시 마지막 데이터 저장
        concurrent_data.save_to_file()

    @app_commands.command(name="동접", description="이터널 리턴 동시 접속자")
    @handle_errors(user_message="동시 접속자 수를 가져오는 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")
    async def concurrent_command(self, interaction: discord.Interaction):
        """현재 이터널 리턴의 동시 접속자 수를 확인합니다."""
        await interaction.response.defer()

        # Steam API 키 확인
        if not config.steam_api_key:
            layout = create_error_layout("지금은 동시 접속자 수를 조회할 수 없습니다. 지원 서버에 알려주세요.")
            layout.add_item(ui.ActionRow(ui.Button(
                style=discord.ButtonStyle.link, label="지원 서버", url=config.support_server, emoji=EMOJIS['support'],
            )))
            # 공개 defer 뒤 첫 followup이라 ephemeral은 적용되지 않는다
            await interaction.followup.send(view=layout)
            return

        current_count = await get_current_player_count()

        if current_count is None:
            layout = create_error_layout("동시 접속자 수를 가져오지 못했습니다. 잠시 후 다시 시도해주세요.")
            await interaction.followup.send(view=layout)
            return

        layout = create_concurrent_layout(current_count)

        await interaction.followup.send(view=layout)

    @tasks.loop(minutes=1)
    async def save_concurrent_data(self):
        """1분마다 동시 접속자 수를 수집하고 저장합니다."""
        try:
            # Steam API 키가 없으면 건너뛰기
            if not config.steam_api_key:
                logger.warning("STEAM_API_KEY가 설정되지 않아 동시접속자 데이터 수집을 건너뜁니다.")
                return

            count = await get_current_player_count()
            if count is not None:
                if self._fail_streak:
                    logger.info(f"동접 수집 복구 ({self._fail_streak}회 연속 실패 후)")
                    self._fail_streak = 0
                concurrent_data.add_data(datetime.now(timezone.utc), count)

                # 5분마다만 파일에 저장 (I/O 부하 감소)
                # len(deque) 기준은 deque가 가득 차면 매분 저장으로 변질된다
                if self.save_concurrent_data.current_loop % 5 == 4:
                    concurrent_data.save_to_file()
            else:
                self._fail_streak += 1
                if self._fail_streak == FAIL_ALERT_THRESHOLD:
                    logger.error(f"동접 수집 {self._fail_streak}회 연속 실패")
                else:
                    logger.warning(f"동시접속자 수를 가져올 수 없어 수집을 건너뜁니다. ({self._fail_streak}회 연속)")
        except Exception as e:
            logger.error(f"동접 데이터 수집 중 오류 발생: {e}", exc_info=True)

    @save_concurrent_data.before_loop
    async def before_save_concurrent_data(self):
        """태스크 시작 전 대기"""
        await self.client.wait_until_ready()
        # 태스크 시작 로그 제거 (불필요한 정보)

async def setup(client):
    """명령어를 등록합니다."""
    await client.add_cog(Concurrent(client))
