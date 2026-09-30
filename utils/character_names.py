"""
실험체 이름 유틸리티
"""
import re
from typing import Dict

from .api_client import api_client
from .config import config
from .logging_config import get_logger

logger = get_logger('실험체명')

# L10N 수신 실패 시 폴백, 기동 후 L10N 값으로 덮어씀
CHARACTER_NAMES: Dict[int, str] = {
    1: "재키", 2: "아야", 3: "피오라", 4: "매그너스", 5: "자히르", 6: "나딘", 7: "현우", 8: "하트", 9: "아이솔", 10: "리 다이린",
    11: "유키", 12: "혜진", 13: "쇼우", 14: "키아라", 15: "시셀라", 16: "실비아", 17: "아드리아나", 18: "쇼이치", 19: "엠마", 20: "레녹스",
    21: "로지", 22: "루크", 23: "캐시", 24: "아델라", 25: "버니스", 26: "바바라", 27: "알렉스", 28: "수아", 29: "레온", 30: "일레븐",
    31: "리오", 32: "윌리엄", 33: "니키", 34: "나타폰", 35: "얀", 36: "이바", 37: "다니엘", 38: "제니", 39: "카밀로", 40: "클로에",
    41: "요한", 42: "비앙카", 43: "셀린", 44: "에키온", 45: "마이", 46: "에이든", 47: "라우라", 48: "띠아", 49: "펠릭스", 50: "엘레나",
    51: "프리야", 52: "아디나", 53: "마커스", 54: "칼라", 55: "에스텔", 56: "피올로", 57: "마르티나", 58: "헤이즈", 59: "아이작", 60: "타지아",
    61: "이렘", 62: "테오도르", 63: "이안", 64: "바냐", 65: "데비&마를렌", 66: "아르다", 67: "아비게일", 68: "알론소", 69: "레니", 70: "츠바메",
    71: "케네스", 72: "카티야", 73: "샬럿", 74: "다르코", 75: "르노어", 76: "가넷", 77: "유민", 78: "히스이", 79: "유스티나", 80: "이슈트반",
    81: "니아", 82: "슈린", 83: "헨리", 84: "블레어", 85: "미르카", 86: "펜리르", 87: "코렐라인", 88: "비형",
    89: "크레이버", 90: "루치아",
}

_L10N_NAME = re.compile(r"^Character/Name/(\d+)┃(.+)$", re.MULTILINE)


async def refresh_character_names() -> None:
    """L10N Korean 파일에서 실험체 이름을 다시 읽는다. 실패하면 기존 값 유지."""
    try:
        meta = await api_client.get(f"{config.api_url}/l10n/Korean", use_cache=False)
        session = await api_client.get_session()
        async with session.get(meta['data']['l10Path']) as response:
            response.raise_for_status()
            text = await response.text(encoding='utf-8')
        names = {int(code): name.strip() for code, name in _L10N_NAME.findall(text)}
    except Exception as e:
        logger.warning(f"실험체 이름 갱신 실패, 기존 목록 유지: {type(e).__name__}: {e}")
        return

    if names:
        CHARACTER_NAMES.update(names)
        logger.info(f"실험체 이름 {len(names)}개 갱신")


def get_character_name(character_code: int) -> str:
    """실험체 코드를 이름으로 변환합니다."""
    return CHARACTER_NAMES.get(character_code, f"실험체{character_code}")
