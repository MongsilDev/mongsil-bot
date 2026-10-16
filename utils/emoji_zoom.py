"""
이모지 확대 기능 모듈
"""
import asyncio
import discord
import json
import os
import re
from typing import Optional, Set, Dict
from pathlib import Path
from utils.logging_config import get_logger
from utils import guild_settings
from utils.usage_db import record_zoom, recent_zoom_guilds

logger = get_logger('이모지확대')

# 설정 파일 경로
SETTINGS_PATH = Path('./data/settings.json')

# 웹훅 캐시 (채널 ID -> 웹훅). 지워진 웹훅은 전송 때 NotFound로 걸러 다시 만든다
_webhook_cache: Dict[int, discord.Webhook] = {}

# 설정 캐시
_disabled_servers_cache: Optional[Set[int]] = None

# 웹훅 권한 오류 알림 캐시 (서버별로 한 번만 알림)
_webhook_permission_notified: Set[int] = set()

_background_tasks: Set[asyncio.Task] = set()

WEBHOOK_NAME = "몽실봇 이모지 확대"
EMOJI_CDN_BASE = "https://cdn.discordapp.com/emojis/"
AVATAR_SIZE = 1024

# 커스텀 이모지 패턴 (여러 개 감지용)
CUSTOM_EMOJI_PATTERN = re.compile(r'<a?:\w+:\d+>')

def load_disabled_servers() -> Set[int]:
    """이모지 확대 기능이 비활성화된 서버 목록을 로드합니다."""
    global _disabled_servers_cache

    if _disabled_servers_cache is not None:
        return _disabled_servers_cache

    try:
        if not SETTINGS_PATH.exists() or SETTINGS_PATH.stat().st_size == 0:
            _disabled_servers_cache = set()
            return _disabled_servers_cache

        with SETTINGS_PATH.open('r', encoding='utf-8') as file:
            data = json.load(file)
            _disabled_servers_cache = set(data)
            return _disabled_servers_cache

    except Exception as e:
        logger.error(f"설정 파일 로드 실패, 빈 목록으로 시작: {e}")
        _disabled_servers_cache = set()
        return _disabled_servers_cache

def save_disabled_servers(disabled_servers: Set[int]) -> bool:
    """비활성화된 서버 목록을 저장합니다. 파손 방지를 위해 임시 파일 후 원자적 교체."""
    global _disabled_servers_cache
    try:
        temp_path = SETTINGS_PATH.with_suffix('.json.tmp')
        with temp_path.open('w', encoding='utf-8') as file:
            json.dump(list(disabled_servers), file, ensure_ascii=False, indent=2)
        os.replace(temp_path, SETTINGS_PATH)
        _disabled_servers_cache = disabled_servers.copy()
        return True
    except Exception as e:
        logger.error(f"설정 파일 저장 중 오류: {e}")
        return False

async def disable_emoji_zoom_and_notify(guild_id: int, channel: discord.TextChannel) -> None:
    """이모지 확대 기능을 비활성화하고 알림을 보냅니다."""
    global _webhook_permission_notified

    # 비활성화는 항상 수행한다. 알림 캐시로 비활성화까지 건너뛰면
    # 저장 실패 후 같은 서버에서 Forbidden을 무한 반복한다.
    disabled_servers = set(load_disabled_servers())
    if guild_id not in disabled_servers:
        disabled_servers.add(guild_id)
        if save_disabled_servers(disabled_servers):
            guild_settings.log(guild_id, "이모지 확대: 끔, 웹후크 관리 권한 없음", source='bot')

    # 알림은 서버당 한 번만
    if guild_id in _webhook_permission_notified:
        return
    _webhook_permission_notified.add(guild_id)

    try:
        from utils.layouts import create_error_layout
        layout = create_error_layout(
            "봇에 웹후크 관리 권한이 없어 이모지 확대를 껐습니다.\n몽실봇 역할에 웹후크 관리 권한을 준 뒤 `/설정`에서 다시 켜주세요.",
            title="이모지 확대 비활성화",
        )
        await channel.send(view=layout)
    except Exception as e:
        logger.error(f"웹훅 권한 오류 알림 전송 실패: {e}")

def _owned(webhook: discord.Webhook, guild: discord.Guild) -> bool:
    return webhook.type == discord.WebhookType.incoming and webhook.user is not None and webhook.user.id == guild.me.id


async def _pick_webhook(channel) -> Optional[discord.Webhook]:
    """몽실봇이 만든 웹훅 하나만 남기고 이름을 맞춘다. 이름이 바뀌었거나 중복으로 생긴 것도 여기서 합친다."""
    owned = [w for w in await channel.webhooks() if _owned(w, channel.guild)]
    if not owned:
        return None
    keep = next((w for w in owned if w.name == WEBHOOK_NAME), owned[0])
    for extra in owned:
        if extra.id != keep.id:
            try:
                await extra.delete(reason="이모지 확대 중복 웹훅 정리")
            except discord.HTTPException as e:
                logger.warning(f"중복 웹훅 삭제 실패 (채널 {channel.id}): {e}")
    if keep.name != WEBHOOK_NAME:
        keep = await keep.edit(name=WEBHOOK_NAME, reason="이모지 확대 웹훅 이름 통일")
    return keep


async def remove_guild_webhooks(guild: discord.Guild) -> None:
    """이모지 확대를 끈 서버에서 몽실봇이 만든 웹훅을 지웁니다."""
    for channel_id in [cid for cid, w in _webhook_cache.items() if w.guild_id == guild.id]:
        _webhook_cache.pop(channel_id, None)
    if not guild.me.guild_permissions.manage_webhooks:
        return
    try:
        webhooks = [w for w in await guild.webhooks() if _owned(w, guild)]
    except discord.HTTPException as e:
        logger.warning(f"웹훅 목록 조회 실패 (서버 {guild.id}): {e}")
        return
    for webhook in webhooks:
        try:
            await webhook.delete(reason="이모지 확대 끔")
        except discord.HTTPException as e:
            logger.warning(f"웹훅 삭제 실패 (서버 {guild.id}): {e}")
    if webhooks:
        logger.info(f"이모지 확대를 꺼 웹훅 {len(webhooks)}개 삭제: 서버 {guild.id}")


def _run_in_background(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def schedule_webhook_removal(guild: discord.Guild) -> None:
    _run_in_background(remove_guild_webhooks(guild))


def schedule_webhook_warmup(client: discord.Client) -> None:
    _run_in_background(warm_webhook_cache(client))


async def get_or_create_webhook(channel) -> Optional[discord.Webhook]:
    """채널의 웹훅을 가져오거나 생성합니다."""
    channel_id = channel.id

    if channel_id in _webhook_cache:
        return _webhook_cache[channel_id]

    try:
        webhook = await _pick_webhook(channel) or await channel.create_webhook(
            name=WEBHOOK_NAME,
            reason="이모지 확대 기능"
        )
        _webhook_cache[channel_id] = webhook
        return webhook

    except discord.Forbidden:
        # 길드 권한 자체가 없으면 서버 비활성화, 채널 오버라이드 탓이면 그 채널만 조용히 스킵
        if not channel.guild.me.guild_permissions.manage_webhooks:
            await disable_emoji_zoom_and_notify(channel.guild.id, channel)
        return None
    except discord.NotFound:
        _webhook_cache.pop(channel_id, None)
        return None
    except discord.HTTPException as e:
        logger.warning(f"웹훅 준비 실패 (채널 {channel_id}): {e}")
        return None

async def warm_webhook_cache(client: discord.Client) -> None:
    """최근 확대가 쓰인 서버의 웹훅을 미리 받아 재시작 뒤 첫 확대가 느려지지 않게 합니다."""
    recent = recent_zoom_guilds(days=30)
    disabled = load_disabled_servers()
    count = 0
    for guild in client.guilds:
        if guild.id not in recent or guild.id in disabled or not guild.me.guild_permissions.manage_webhooks:
            continue
        try:
            hooks = await guild.webhooks()
        except discord.HTTPException as e:
            logger.warning(f"웹훅 미리 받기 실패 (서버 {guild.id}): {e}")
            continue
        by_channel: Dict[int, list] = {}
        for w in hooks:
            if _owned(w, guild):
                by_channel.setdefault(w.channel_id, []).append(w)
        # 중복이 있는 채널은 비워 두어 첫 확대 때 _pick_webhook이 합치게 한다
        for channel_id, owned in by_channel.items():
            if len(owned) == 1 and owned[0].name == WEBHOOK_NAME:
                _webhook_cache.setdefault(channel_id, owned[0])
                count += 1
        await asyncio.sleep(0.2)
    logger.info(f"이모지 확대 웹훅 {count}개 미리 받음")


async def _delete_original(message: discord.Message) -> bool:
    try:
        await message.delete()
        return True
    except (discord.Forbidden, discord.NotFound):
        return False  # 메시지 관리 권한이 없으면 원본과 확대본이 함께 남는다
    except discord.HTTPException as e:
        logger.warning(f"원본 이모지 삭제 실패 (채널 {message.channel.id}): {e}")
        return False


async def process_emoji_zoom(message: discord.Message) -> None:
    """이모지 확대 기능을 처리합니다."""
    # 스레드와 포럼 글은 부모 채널의 웹훅으로 보낸다
    thread = message.channel if isinstance(message.channel, discord.Thread) else None
    hook_channel = thread.parent if thread else message.channel
    if not isinstance(hook_channel, (discord.TextChannel, discord.VoiceChannel, discord.ForumChannel)):
        return

    guild_id = message.guild.id
    if guild_id in load_disabled_servers() or not guild_settings.zoom_allowed(guild_id, hook_channel.id):
        return

    content = message.content.strip()
    length = len(content)
    if (length < 10 or length > 100 or
        content[0] != '<' or content[-1] != '>' or ':' not in content):
        return

    # 커스텀 이모지가 2개 이상이면 확대하지 않음
    emoji_matches = CUSTOM_EMOJI_PATTERN.findall(content)
    if len(emoji_matches) != 1:
        return

    # 이모지 외에 다른 텍스트가 있으면 확대하지 않음
    if content != emoji_matches[0]:
        return

    try:
        emoji = discord.PartialEmoji.from_str(content)
        if not emoji:
            return
    except (ValueError, AttributeError):
        return

    webhook = await get_or_create_webhook(hook_channel)
    if not webhook:
        return

    # 이모지 URL 생성 (원본 화질)
    emoji_url = f"{EMOJI_CDN_BASE}{emoji.id}.{'gif' if emoji.animated else 'png'}"

    # 프로필 사진 URL 처리 (1024px, GIF 미지원)
    avatar_url = str(message.author.display_avatar.replace(size=AVATAR_SIZE, format='png'))

    async def send_zoom(hook: discord.Webhook) -> None:
        await hook.send(
            content=emoji_url,
            username=message.author.display_name,
            avatar_url=avatar_url,
            thread=thread or discord.utils.MISSING,
            wait=False
        )

    # 원본 삭제와 확대본 전송을 동시에 보내 왕복 한 번을 줄인다
    deleting = asyncio.create_task(_delete_original(message))
    sent = True
    try:
        await send_zoom(webhook)
    except discord.NotFound:
        # 웹훅이 수동 삭제된 경우: 캐시 비우고 한 번 재생성해 재시도
        _webhook_cache.pop(hook_channel.id, None)
        webhook = await get_or_create_webhook(hook_channel)
        try:
            if not webhook:
                raise discord.ClientException("웹훅 재생성 실패")
            await send_zoom(webhook)
        except (discord.HTTPException, discord.ClientException) as e:
            logger.warning(f"확대 이모지 재전송 실패 (채널 {message.channel.id}): {e}")
            sent = False
    except discord.HTTPException as e:
        logger.warning(f"확대 이모지 전송 실패 (채널 {message.channel.id}): {e}")
        sent = False

    if not sent and await deleting:
        # 원본만 지워진 경우 봇 이름으로라도 확대본을 남긴다
        try:
            await message.channel.send(f"**{discord.utils.escape_markdown(message.author.display_name)}**\n{emoji_url}",
                                       allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            logger.warning(f"확대 이모지 대체 전송 실패 (채널 {message.channel.id}): {e}")
        return
    if sent:
        record_zoom(guild_id, message.author.id)
    await deleting

async def cleanup_emoji_zoom_cache():
    """이모지 확대 관련 캐시를 정리합니다."""
    global _webhook_cache, _disabled_servers_cache, _webhook_permission_notified
    _webhook_cache.clear()
    _disabled_servers_cache = None
    _webhook_permission_notified.clear()
    logger.info("이모지 확대 관련 캐시가 정리되었습니다.")
