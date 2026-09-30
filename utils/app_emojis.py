"""
봇 전용 이모지. 실험체 얼굴과 티어 아이콘, 이름은 영어. 디스코드가 한글 이름을 받지 않음
"""
from typing import Dict

import discord

from .api_client import api_client
from .logging_config import get_logger

logger = get_logger('앱이모지')

CHARACTERS_URL = 'https://er.dakgg.io/api/v1/data/characters?hl=ko-KR'

EMOJI: Dict[str, str] = {}

# 실험체 코드별 이모지 이름. 새 실험체는 sync_characters가 추가
CHARACTER_KEYS: Dict[int, str] = {
    1: 'Jackie', 2: 'Aya', 3: 'Fiora', 4: 'Magnus', 5: 'Zahir', 6: 'Nadine', 7: 'Hyunwoo', 8: 'Hart',
    9: 'Isol', 10: 'LiDailin', 11: 'Yuki', 12: 'Hyejin', 13: 'Xiukai', 14: 'Chiara', 15: 'Sissela',
    16: 'Silvia', 17: 'Adriana', 18: 'Shoichi', 19: 'Emma', 20: 'Lenox', 21: 'Rozzi', 22: 'Luke',
    23: 'Cathy', 24: 'Adela', 25: 'Bernice', 26: 'Barbara', 27: 'Alex', 28: 'Sua', 29: 'Leon', 30: 'Eleven',
    31: 'Rio', 32: 'William', 33: 'Nicky', 34: 'Nathapon', 35: 'Jan', 36: 'Eva', 37: 'Daniel', 38: 'Jenny',
    39: 'Camilo', 40: 'Chloe', 41: 'Johann', 42: 'Bianca', 43: 'Celine', 44: 'Echion', 45: 'Mai',
    46: 'Aiden', 47: 'Laura', 48: 'Tia', 49: 'Felix', 50: 'Elena', 51: 'Priya', 52: 'Adina', 53: 'Markus',
    54: 'Karla', 55: 'Estelle', 56: 'Piolo', 57: 'Martina', 58: 'Haze', 59: 'Isaac', 60: 'Tazia', 61: 'Irem',
    62: 'Theodore', 63: 'Lyanh', 64: 'Vanya', 65: 'DebiMarlene', 66: 'Arda', 67: 'Abigail', 68: 'Alonso',
    69: 'Leni', 70: 'Tsubame', 71: 'Kenneth', 72: 'Katja', 73: 'Charlotte', 74: 'Darko', 75: 'Lenore',
    76: 'Garnet', 77: 'YuMin', 78: 'Hisui', 79: 'Justyna', 80: 'Istvan', 81: 'Niah', 82: 'Xuelin',
    83: 'Henry', 84: 'Blair', 85: 'Mirka', 86: 'Fenrir', 87: 'Coraline', 88: 'Bihyung', 89: 'Craver',
    90: 'Lucia',
}

TIER_KEYS = ['Unranked', 'Iron', 'Bronze', 'Silver', 'Gold', 'Platinum', 'Diamond', 'Meteorite', 'Mithril', 'Demigod', 'Eternity']


def character(code) -> str:
    return EMOJI.get(CHARACTER_KEYS.get(int(code), ''), '')


def tier(icon) -> str:
    index = int(icon)
    return EMOJI.get(TIER_KEYS[index], '') if 0 <= index < len(TIER_KEYS) else ''


async def load(client: discord.Client) -> None:
    try:
        for emoji in await client.fetch_application_emojis():
            EMOJI[emoji.name] = str(emoji)
        logger.info(f"앱 이모지 {len(EMOJI)}개")
    except discord.HTTPException as e:
        logger.warning(f"앱 이모지 목록 조회 실패: {e}")


async def sync_characters(client: discord.Client) -> None:
    """새 실험체 얼굴 등록. 실패하면 이름만 표시"""
    try:
        data = await api_client.get(CHARACTERS_URL, use_cache=False)
        session = await api_client.get_session()
        for char in data.get('characters', []):
            name = char['key']
            CHARACTER_KEYS.setdefault(char['id'], name)
            if name in EMOJI or not char.get('communityImageUrl'):
                continue
            async with session.get('https:' + char['communityImageUrl']) as response:
                response.raise_for_status()
                image = await response.read()
            emoji = await client.create_application_emoji(name=name, image=image)
            EMOJI[name] = str(emoji)
            logger.info(f"앱 이모지 추가 {name} {char.get('name')}")
    except Exception as e:
        logger.warning(f"실험체 이모지 동기화 실패: {type(e).__name__}: {e}")
