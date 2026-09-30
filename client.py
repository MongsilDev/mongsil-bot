import discord
from discord.ext import commands, tasks
from discord import app_commands
from typing import Optional
from datetime import datetime, timedelta
import math
import os

from utils.config import config
from utils.layouts import create_error_layout
from utils.logging_config import get_logger
from utils.api_client import api_client
from utils.character_names import refresh_character_names
from utils import app_emojis
from utils.usage_db import record_command, record_guild_event

logger = get_logger(__name__)


class ERClient(commands.Bot):
    def __init__(self, intents: discord.Intents = None):
        if intents is None:
            intents = discord.Intents.default()
            intents.message_content = True
            intents.presences = False

        super().__init__(command_prefix="!", intents=intents, activity=discord.Game(name="이터널 리턴"))

        # data 디렉토리 생성
        os.makedirs("data", exist_ok=True)

        self.api_client = api_client
        self.start_time = None  # main.py에서 설정됨
        self.dashboard = None

        # 이름만 on_tree_error인 메서드는 아무 데도 연결되지 않는다. 명시적으로 바인딩해야 동작.
        self.tree.on_error = self.on_tree_error

    @property
    def uptime(self) -> Optional[timedelta]:
        """봇의 업타임을 반환합니다."""
        if self.start_time is None:
            return None
        return datetime.now() - self.start_time

    async def get_user_nickname(self, nickname: str) -> Optional[str]:
        """닉네임으로 유저 UID를 조회합니다.

        None은 '없는 닉네임'만 의미한다. API 장애는 예외로 전파해
        '닉네임을 확인해주세요' 오안내가 나가지 않게 한다.
        """
        url = f"{config.api_url}/user/nickname"
        params = {'query': nickname}
        # 닉네임-uid 매핑은 사실상 불변이라 길게 캐시
        response = await self.api_client.get(url, params=params, ttl=86400)

        if response and response.get('code') == 200:
            user_data = response.get('user')
            if user_data:
                # API 문서와 실제 응답의 필드명 차이 대응
                for field in ('userNum', 'userId', 'uid'):
                    if field in user_data:
                        return user_data[field]

        # '없음' 응답(HTTP 200 + code 404)까지 24시간 캐시하면
        # 신규 생성이나 개명 직후 유저가 하루 동안 조회 불가가 된다
        self.api_client.uncache(url, params=params)
        return None

    async def setup_hook(self) -> None:
        try:
            for module in [
                "commands.season",
                "commands.concurrent",
                "commands.playtime",
                "commands.dog",
                "commands.cat",
                "commands.ranking",
                "commands.settings",
                "commands.info",
                "commands.rank",
                "commands.rating"
            ]:
                await self.load_extension(module)

            await app_emojis.load(self)
            self.refresh_names.start()

            if os.getenv('DASHBOARD_URL') and os.getenv('DASHBOARD_CLIENT_SECRET'):
                # 대시보드 설정 오류로 봇까지 내려가지 않게 여기서 끊음
                try:
                    from web.server import start_dashboard
                    self.dashboard = await start_dashboard(self)
                except Exception:
                    logger.error("대시보드 시작 실패", exc_info=True)
            else:
                logger.info("대시보드 꺼짐 (DASHBOARD_URL, DASHBOARD_CLIENT_SECRET 필요)")

            # 커맨드 동기화는 SYNC_COMMANDS=1 환경변수가 설정된 경우에만 수행
            # 매 재시작마다 sync하면 Discord rate limit에 걸려 연결 끊김/재연결 반복 발생
            if os.getenv('SYNC_COMMANDS') == '1':
                await self.tree.sync()
                logger.info("슬래시 커맨드 동기화 완료")
            else:
                logger.info("슬래시 커맨드 동기화 건너뜀 (SYNC_COMMANDS=1로 설정하면 동기화)")

        except Exception:
            raise

    @tasks.loop(hours=24)
    async def refresh_names(self):
        """시즌 중 추가되는 신규 실험체 이름 반영"""
        await refresh_character_names()
        await app_emojis.sync_characters(self)

    async def on_disconnect(self):
        """봇이 연결이 끊어졌을 때 호출됩니다."""
        logger.warning("봇 연결이 끊어졌습니다.")

    async def on_resumed(self):
        """봇이 재연결되었을 때 호출됩니다."""
        logger.info("봇이 재연결되었습니다.")

    async def on_guild_join(self, guild):
        """봇이 새 서버에 참가했을 때 호출됩니다."""
        logger.info(f"서버 참가: {guild.name} (ID: {guild.id}, 멤버: {guild.member_count})")
        record_guild_event(guild.id, guild.name, guild.member_count, 'join')

    async def on_guild_remove(self, guild):
        """봇이 서버에서 제거되었을 때 호출됩니다."""
        logger.info(f"서버 제거: {guild.name} (ID: {guild.id})")
        record_guild_event(guild.id, guild.name, guild.member_count, 'leave')

    async def on_app_command_completion(self, interaction: discord.Interaction, command):
        # handle_errors가 잡은 실패도 여기로 온다. 상태는 extras에 표시됨
        ms = (discord.utils.utcnow() - interaction.created_at).total_seconds() * 1000
        record_command(interaction.guild_id, interaction.user.id, command.qualified_name,
                       interaction.extras.get('status', 'ok'), int(ms))

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        error_message = "명령어 실행 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."

        if isinstance(error, app_commands.CheckFailure):
            error_message = "이 명령어를 실행할 권한이 없습니다."
        elif isinstance(error, app_commands.CommandOnCooldown):
            error_message = f"명령어를 너무 자주 사용했습니다. {math.ceil(error.retry_after)}초 후에 다시 시도해주세요."

        # 권한/쿨다운은 예상된 유저 조건이라 WARNING (Sentry는 ERROR 이상만 수집)
        blocked = isinstance(error, (app_commands.CheckFailure, app_commands.CommandOnCooldown))
        if blocked:
            logger.warning(f"명령어 차단: {error}")
        else:
            logger.error(f"명령어 실행 오류: {error}", exc_info=True)

        if interaction.command:
            ms = (discord.utils.utcnow() - interaction.created_at).total_seconds() * 1000
            record_command(interaction.guild_id, interaction.user.id, interaction.command.qualified_name,
                           'user' if blocked else 'error', int(ms))

        try:
            layout = create_error_layout(error_message)
            if not interaction.response.is_done():
                await interaction.response.send_message(view=layout, ephemeral=True)
            else:
                await interaction.followup.send(view=layout, ephemeral=True)
        except discord.NotFound:
            logger.debug("에러 응답 전송 생략: 만료된 인터랙션")
        except discord.HTTPException as e:
            logger.warning(f"에러 응답 전송 실패: {e}")

    async def close(self):
        """봇 종료 시 리소스를 정리합니다."""
        try:
            if self.dashboard:
                await self.dashboard.cleanup()
            await self.api_client.close()
            logger.info("리소스 정리 완료")
        except Exception as e:
            logger.error(f"리소스 정리 중 오류: {e}")
        finally:
            await super().close()
