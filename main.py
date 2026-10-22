import discord
from client import ERClient
from datetime import datetime

import os

import sentry_sdk

from utils.config import config  # import 시점에 load_dotenv()가 실행된다
from utils.errors import is_transient, redact_secrets
from utils.logging_config import setup_logging, get_logger
from utils.emoji_zoom import process_emoji_zoom, cleanup_emoji_zoom_cache, schedule_webhook_warmup

# 장애 추적. config import로 .env가 로드된 뒤여야 DSN 값을 읽을 수 있음
# DSN이 비어 있으면 transport가 없어 어디로도 전송되지 않는다.
_RECONNECT_LOGGERS = ("discord.client", "discord.gateway")


def _sentry_before_send(event, hint):
    """일시적 네트워크 에러와 discord.py 재연결 노이즈는 Sentry로 보내지 않는다.

    판정은 예외 클래스와 로거 이름으로만 한다. 메시지 문구로 거르면
    같은 문구를 쓰는 설정 오류까지 함께 사라진다.
    """
    exc_info = hint.get("exc_info")
    if exc_info and is_transient(exc_info[1]):
        return None

    logentry = event.get("logentry") or {}
    if event.get("logger") in _RECONNECT_LOGGERS:
        msg = logentry.get("message") or event.get("message") or ""
        if str(msg).startswith("Attempting a reconnect"):
            return None

    return _redact_event(event, hint)


def _redact_event(event, hint):
    """오류 이벤트와 성능 추적 공용. URL 자격증명이 남지 않게 가림"""
    logentry = event.get("logentry") or {}
    for k in ("message", "formatted"):
        if logentry.get(k):
            logentry[k] = redact_secrets(logentry[k])
    if isinstance(logentry.get("params"), list):
        logentry["params"] = [redact_secrets(p) if isinstance(p, str) else p for p in logentry["params"]]
    if event.get("message"):
        event["message"] = redact_secrets(event["message"])
    for exc in (event.get("exception") or {}).get("values") or []:
        if exc.get("value"):
            exc["value"] = redact_secrets(exc["value"])
    # /auth/callback 쿼리에 OAuth code와 state가 있음
    request = event.get("request") or {}
    if isinstance(request.get("query_string"), str):
        request["query_string"] = redact_secrets(request["query_string"])
    headers = request.get("headers") or {}
    for k, v in headers.items():
        if isinstance(v, str):
            headers[k] = redact_secrets(v)
    # aiohttp 브레드크럼과 span은 data의 url, http.query에 요청 주소가 그대로 있음
    for item in ((event.get("breadcrumbs") or {}).get("values") or []) + (event.get("spans") or []):
        for k in ("message", "description"):
            if isinstance(item.get(k), str):
                item[k] = redact_secrets(item[k])
        data = item.get("data") or {}
        for k in ("url", "http.query"):
            if isinstance(data.get(k), str):
                data[k] = redact_secrets(data[k])
    return event


def _traces_sampler(ctx):
    """요청이 보낸 sentry-trace, baggage로는 샘플링하지 않음"""
    request = ctx.get("aiohttp_request")
    if request is not None and ("sentry-trace" in request.headers or "baggage" in request.headers):
        return 0
    return 0.1


sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN", ""),
    traces_sampler=_traces_sampler,
    # Discord, bser 같은 외부 요청에 sentry-trace와 baggage 헤더를 붙이지 않음
    trace_propagation_targets=[],
    before_send=_sentry_before_send,
    before_send_transaction=_redact_event,
    # 대시보드 OAuth 핸들러 지역변수에 액세스 토큰이 있음
    include_local_variables=False,
)

# 로깅 설정
# 컨테이너에서는 supervisor가 stdout을 저장하므로 파일 로그는 LOG_FILE을 줄 때만
setup_logging(level="INFO", log_file=os.getenv("LOG_FILE"))
logger = get_logger()

# Initialize bot
# members 인텐트는 켜지 않는다. 유일한 용도가 /정보 유저 수 중복 제거였는데
# 그 대가로 기동 시 길드 청킹에 4분 30초가 걸렸다.
intents = discord.Intents.default()
intents.message_content = True
intents.presences = False

# 음성 기능을 쓰지 않아 PyNaCl, davey 미설치 경고를 끔
discord.VoiceClient.warn_nacl = False
discord.VoiceClient.warn_dave = False

client = ERClient(intents=intents)


@client.event
async def on_ready():
    """봇이 준비되었을 때 실행됩니다. 재연결 시에도 다시 불린다."""
    if client.start_time is None:
        client.start_time = datetime.now()
        await cleanup_emoji_zoom_cache()
        schedule_webhook_warmup(client)

    logger.info(f"{client.user} 온라인")

@client.event
async def on_message(message: discord.Message):
    """메시지를 수신했을 때 실행됩니다."""
    # 기본 필터링
    if not message.guild or message.author.bot:
        return
    
    # 이모지 확대 기능 처리
    await process_emoji_zoom(message)

if __name__ == "__main__":
    try:
        # log_handler 기본값은 루트 로거를 덮어쓰므로 자체 설정을 유지한다
        client.run(config.bot_token, log_handler=None)
    except KeyboardInterrupt:
        logger.warning("사용자 중단")
    except Exception as e:
        logger.error(f"봇 실행 실패: {e}", exc_info=True)
        import sys
        sys.exit(1)