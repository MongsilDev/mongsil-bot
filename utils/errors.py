"""
에러 처리 유틸리티
"""
import re

import discord
from typing import Optional, Callable
from functools import wraps
from .layouts import create_error_layout
from .logging_config import get_logger

logger = get_logger('errors')

# aiohttp 예외 문자열에는 요청 URL이 쿼리까지 통째로 들어온다(Steam key= 등).
_SECRET_QS = re.compile(r"(key|apikey|api_key)=[^&\s'\"]+", re.IGNORECASE)


def redact_secrets(text) -> str:
    """로그, 에러 메시지에 실릴 문자열에서 URL 쿼리 자격증명을 가린다."""
    return _SECRET_QS.sub(r"\1=****", str(text))


TRANSIENT_EXC_NAMES = frozenset((
    "TimeoutError", "ConnectTimeoutError", "ReadTimeout", "ConnectionError",
    "ServerTimeoutError", "ConnectionTimeoutError", "SocketTimeoutError",
    "ClientConnectorError", "ClientConnectorDNSError", "ClientConnectionResetError",
    "ClientOSError", "ServerDisconnectedError", "ServiceDownError",
    "WSServerHandshakeError", "ConnectionClosed", "ConnectionResetError",
))


def is_transient(exc: Optional[BaseException]) -> bool:
    """예외나 그 원인 체인에 일시적 네트워크, 타임아웃 예외가 있는지 본다.

    from None으로 감춘 원인도 __context__에 남아 있어 함께 검사한다.
    """
    for _ in range(5):
        if exc is None:
            return False
        if type(exc).__name__ in TRANSIENT_EXC_NAMES:
            return True
        exc = exc.__cause__ or exc.__context__
    return False

class BotError(Exception):
    """봇 관련 기본 예외 클래스"""
    def __init__(self, message: str, user_message: Optional[str] = None):
        self.message = message
        self.user_message = user_message or "오류가 발생했습니다. 잠시 후 다시 시도해주세요."
        super().__init__(self.message)

class APIError(BotError):
    """API 관련 예외"""
    pass

class ValidationError(BotError):
    """입력 검증 예외"""
    pass

class ServiceDownError(BotError):
    """게임 점검이나 장애로 조회가 멈춘 상태. 일시 장애로 분류해 WARNING으로 남김"""
    pass


class NotFoundError(BotError):
    """데이터를 찾을 수 없는 예외"""
    pass


class InvalidUidError(NotFoundError):
    """uid로 조회가 안 되는 계정. 탈퇴했거나 닉네임을 바꿔 uid가 달라진 경우"""
    pass


def _find_interaction(args) -> Optional[discord.Interaction]:
    """데코레이터가 감싼 함수 인자에서 Interaction을 찾는다."""
    for arg in args:
        if isinstance(arg, discord.Interaction):
            return arg
    return None


async def _send_error(interaction: discord.Interaction, error_text: str):
    """에러 LayoutView를 전송합니다. 로딩 메시지가 있으면 교체합니다."""
    layout = create_error_layout(error_text)
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(view=layout, ephemeral=True)
        else:
            # 로딩 메시지를 에러로 교체 시도, 실패 시 followup
            try:
                await interaction.edit_original_response(view=layout, embeds=[], attachments=[])
            except discord.HTTPException:
                await interaction.followup.send(view=layout, ephemeral=True)
    except discord.NotFound:
        logger.debug("에러 응답 전송 생략: 만료된 인터랙션")
    except discord.HTTPException as e:
        logger.warning(f"에러 응답 전송 실패: {e}")


def handle_errors(
    user_message: str = "명령어 실행 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요.",
    log_error: bool = True
):
    """에러 처리를 위한 데코레이터"""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except BotError as e:
                if log_error:
                    # 오타 조회, 기록 없음 같은 예상된 유저 조건은 WARNING으로 남긴다.
                    # Sentry는 ERROR 이상만 수집하므로 노이즈가 되지 않는다.
                    if isinstance(e, (NotFoundError, ValidationError)) or is_transient(e):
                        logger.warning(f"BotError in {func.__name__}: {e.message}")
                    else:
                        logger.error(f"BotError in {func.__name__}: {e.message}")

                interaction = _find_interaction(args)
                if interaction:
                    user_fault = isinstance(e, (NotFoundError, ValidationError))
                    interaction.extras['status'] = 'user' if user_fault else 'error'
                    await _send_error(interaction, e.user_message)

                return None
            except Exception as e:
                # 10062는 응답 기한이 지난 인터랙션이라 답할 곳이 없음
                if isinstance(e, discord.NotFound) and e.code == 10062:
                    logger.warning(f"만료된 인터랙션 {func.__name__}: {e}")
                    return None
                if log_error:
                    logger.error(f"Unexpected error in {func.__name__}: {e}", exc_info=True)

                interaction = _find_interaction(args)
                if interaction:
                    interaction.extras['status'] = 'error'
                    await _send_error(interaction, user_message)

                return None
        return wrapper
    return decorator

def validate_nickname(nickname: str) -> str:
    """닉네임 입력을 검증합니다."""
    if not nickname or not nickname.strip():
        raise ValidationError("닉네임이 비어있습니다.", "닉네임을 입력해주세요.")

    nickname = nickname.strip()

    if len(nickname) < 2:
        raise ValidationError("닉네임이 너무 짧습니다.", "닉네임은 2자 이상 입력해주세요.")

    if len(nickname) > 20:
        raise ValidationError("닉네임이 너무 깁니다.", "닉네임은 20자 이하로 입력해주세요.")

    # 특수 문자 검증 (기본적인 것만)
    forbidden_chars = ['<', '>', '@', '#', '&', '!', '`', '*', '_', '~', '|', '\\']
    for char in forbidden_chars:
        if char in nickname:
            raise ValidationError(f"닉네임에 사용할 수 없는 문자가 포함되어 있습니다: {char}",
                                f"닉네임에 특수문자 '{char}'는 쓸 수 없습니다.\n영문, 한글, 숫자만 입력해주세요.")

    return nickname
