"""
LayoutView 기반 공통 레이아웃 유틸리티
discord.py 2.7+ Components V2 사용
"""
import asyncio
import logging
import time
import discord
from discord import ui
from typing import Optional
from .emojis import EMOJIS


def create_error_layout(description: str, title: Optional[str] = None) -> ui.LayoutView:
    """에러 메시지용 LayoutView를 생성합니다. 상태는 빨간 액센트가 전달한다."""
    text = f"### {title}\n{description}" if title else description
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(ui.TextDisplay(text), accent_colour=discord.Colour.red()))
    return view


def create_loading_layout(title: str) -> ui.LayoutView:
    """로딩 메시지용 LayoutView를 생성합니다."""
    view = ui.LayoutView(timeout=None)
    view.add_item(ui.Container(ui.TextDisplay(f"### {EMOJIS['loading']} {title}"), accent_colour=discord.Colour.blurple()))
    return view


class CooldownLayoutView(ui.LayoutView):
    """상호작용 쿨다운이 적용된 LayoutView 베이스 클래스.

    버튼 등의 상호작용에 유저별 1초 쿨다운을 적용합니다.
    서브클래스에서 interaction_check를 오버라이드할 때 반드시
    super().interaction_check()을 먼저 호출하세요.

    전송 후 self.message에 메시지를 넣어두면 타임아웃 시 버튼을
    비활성화합니다. 안 넣으면 만료된 버튼이 '상호작용 실패'로 남습니다.
    """

    COOLDOWN_SECONDS = 1.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cooldowns: dict = {}
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        user_id = interaction.user.id
        now = time.monotonic()
        last_time = self._cooldowns.get(user_id, 0)

        if now - last_time < self.COOLDOWN_SECONDS:
            await interaction.response.defer()
            return False

        self._cooldowns[user_id] = now

        # 원 인터랙션 토큰은 15분이면 만료된다. 컴포넌트 상호작용의 메시지는
        # 봇 토큰으로 편집되므로 갱신해 두면 늦은 타임아웃 편집도 성공한다.
        if interaction.message is not None:
            self.message = interaction.message

        return True

    async def on_timeout(self) -> None:
        if not self.message:
            return

        changed = False
        for child in self.walk_children():
            if (isinstance(child, ui.Select) or isinstance(child, ui.Button) and child.style != discord.ButtonStyle.link) \
                    and not child.disabled:
                child.disabled = True
                changed = True

        if changed:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


def mark_shown(interaction: discord.Interaction) -> None:
    """결과 카드가 화면에 뜬 시점을 명령어 기록에 남김"""
    interaction.extras['shown_ms'] = int((discord.utils.utcnow() - interaction.created_at).total_seconds() * 1000)


async def sent_message(interaction: discord.Interaction, sent) -> discord.InteractionMessage:
    """send_message 응답에 담긴 메시지. original_response()는 매번 GET을 한 번 더 보냄"""
    if isinstance(sent.resource, discord.InteractionMessage):
        return sent.resource
    return await interaction.original_response()


# 이 안에 결과가 준비되면 로딩 카드 없이 바로 보냄. 디스코드 왕복 한 번이 약 0.3초
FAST_REPLY_SECONDS = 1.0


async def send_card(interaction: discord.Interaction, loading: str, build):
    """결과가 빨리 나오면 바로 보내고, 늦으면 로딩 카드를 먼저 보낸 뒤 교체. 이미 응답한 인터랙션은 새 메시지로"""
    from . import visual
    if not interaction.response.is_done():
        task = asyncio.ensure_future(build())
        try:
            view = await asyncio.wait_for(asyncio.shield(task), FAST_REPLY_SECONDS)
        except asyncio.TimeoutError:
            await interaction.response.send_message(view=create_loading_layout(loading))
            view = await task
            message = await interaction.edit_original_response(view=view, embeds=[], attachments=visual.files_of(view))
        else:
            message = await sent_message(interaction, await interaction.response.send_message(
                view=view, files=visual.files_of(view)))
        mark_shown(interaction)
        return view, message
    from .errors import BotError
    message = await interaction.followup.send(view=create_loading_layout(loading), wait=True)
    try:
        view = await build()
    except BotError as e:
        # 원 응답은 계정 카드라 오류로 덮지 않고 로딩 메시지를 오류로 바꿈
        await message.edit(view=create_error_layout(e.user_message))
        return None, message
    except Exception:
        logging.getLogger('mongsil-bot.layouts').error("카드 생성 실패", exc_info=True)
        await message.edit(view=create_error_layout("조회 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."))
        return None, message
    await message.edit(view=view, attachments=visual.files_of(view))
    mark_shown(interaction)
    return view, message
