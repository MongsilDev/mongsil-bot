"""
봇 전용 이모지. 캐릭터 얼굴 c{코드}, 티어 아이콘 t{아이콘 번호}
"""
from typing import Dict

import discord

from .api_client import api_client
from .logging_config import get_logger

logger = get_logger('앱이모지')

CHARACTERS_URL = 'https://er.dakgg.io/api/v1/data/characters?hl=ko-KR'

EMOJI: Dict[str, str] = {}


def character(code) -> str:
    return EMOJI.get(f'c{code}', '')


def tier(icon) -> str:
    return EMOJI.get(f't{icon}', '')


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
            name = f"c{char['id']}"
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
