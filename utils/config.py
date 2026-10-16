"""
중앙화된 설정 관리 모듈
"""
import os
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv

# 환경 변수 로드
load_dotenv()

@dataclass
class BotConfig:
    """봇 설정을 관리하는 데이터 클래스"""
    # 필수 설정
    bot_token: str
    api_key: str

    # 선택적 설정 (기본값 포함)
    retry_delay: int = 1
    cache_ttl: int = 3600
    api_url: str = 'https://open-api.bser.io/v1'
    steam_api_key: Optional[str] = None
    appid_erbs: int = 1049590

    # 개발자 정보
    developer_tag: str = 'mongsil.dev'
    support_server: str = 'https://discord.gg/4QSFVsNNkE'
    bot_invite: str = 'https://discord.com/oauth2/authorize?client_id=1118780504490131557'
    dashboard_url: str = os.getenv('DASHBOARD_URL', 'https://bot.mongsil.dev')

    # UI 타임아웃 설정 (초 단위)
    view_timeout_interactive: int = 300  # 상호작용 버튼 (5분)

    @classmethod
    def from_env(cls) -> 'BotConfig':
        """환경 변수에서 BotConfig 객체를 생성합니다."""
        # 필수 환경 변수 검증
        required_vars = {
            'BOT_TOKEN': os.getenv('BOT_TOKEN'),
            'API_KEY': os.getenv('API_KEY'),
        }

        missing_vars = [var for var, value in required_vars.items() if not value]
        if missing_vars:
            raise ValueError(f"필수 환경 변수가 설정되지 않았습니다: {', '.join(missing_vars)}")

        return cls(
            bot_token=required_vars['BOT_TOKEN'],
            api_key=required_vars['API_KEY'],
            steam_api_key=os.getenv('STEAM_API_KEY'),
        )

# 전역 설정 인스턴스
config = BotConfig.from_env()
