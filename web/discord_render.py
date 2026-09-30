"""
봇이 보내는 LayoutView를 디스코드 다크 테마와 비슷한 HTML로 변환. 웹 미리보기용
"""
import base64
import html
import re
from datetime import datetime, timedelta, timezone

from discord import ButtonStyle, ui

from utils.visual import attached

KST = timezone(timedelta(hours=9))

INLINE = [
    (re.compile(r'\*\*(.+?)\*\*'), r'<strong>\1</strong>'),
    (re.compile(r'`([^`]+)`'), r'<code>\1</code>'),
]
TIMESTAMP = re.compile(r'&lt;t:(\d+):([tTdDfFR])&gt;')
CUSTOM_EMOJI = re.compile(r'&lt;(a?):(\w+):(\d+)&gt;')


def _timestamp(m: re.Match) -> str:
    t = datetime.fromtimestamp(int(m[1]), KST)
    style = m[2]
    if style == 't':
        text = f"{'오전' if t.hour < 12 else '오후'} {t.hour % 12 or 12}:{t.minute:02d}"
    elif style == 'd':
        text = f"{t.year}. {t.month}. {t.day}."
    else:
        text = f"{t.year}년 {t.month}월 {t.day}일 {t:%H:%M}"
    return f'<span class="dc-time">{text}</span>'


def _inline(text: str) -> str:
    out = html.escape(text, quote=False)
    for pattern, repl in INLINE:
        out = pattern.sub(repl, out)
    out = CUSTOM_EMOJI.sub(lambda m: f'<img class="dc-custom-emoji" src="https://cdn.discordapp.com/emojis/{m[3]}.{"gif" if m[1] else "png"}" alt="">', out)
    return TIMESTAMP.sub(_timestamp, out)


def markdown(text: str) -> str:
    lines = []
    for line in text.split('\n'):
        for prefix, tag in (('### ', 'h3'), ('## ', 'h2'), ('# ', 'h1'), ('-# ', 'small')):
            if line.startswith(prefix):
                lines.append(f'<div class="dc-{tag}">{_inline(line[len(prefix):])}</div>')
                break
        else:
            lines.append(f'<div class="dc-line">{_inline(line) or "&nbsp;"}</div>')
    return ''.join(lines)


def _button(b: ui.Button) -> str:
    style = {
        ButtonStyle.primary: 'primary', ButtonStyle.success: 'success',
        ButtonStyle.danger: 'danger', ButtonStyle.link: 'secondary',
    }.get(b.style, 'secondary')
    emoji = f'<span class="dc-emoji">{html.escape(str(b.emoji))}</span>' if b.emoji else ''
    label = html.escape(b.label or '')
    external = ('<svg class="dc-ext" width="14" height="14" viewBox="0 0 24 24" aria-hidden="true">'
                '<path fill="currentColor" d="M15 3h6v6h-2V6.41l-7.29 7.3-1.42-1.42L17.59 5H15V3ZM5 5h6v2H5v12h12v-6h2v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z"/></svg>'
                if b.style == ButtonStyle.link else '')
    disabled = ' is-disabled' if b.disabled else ''
    return f'<span class="dc-button dc-{style}{disabled}">{emoji}{label}{external}</span>'


def _media(url: str, files: dict) -> str:
    name = url.removeprefix('attachment://')
    if url.startswith('attachment://') and name in files:
        return 'data:image/png;base64,' + base64.b64encode(files[name]).decode()
    return url


def _item(item, files: dict) -> str:
    if isinstance(item, ui.TextDisplay):
        return f'<div class="dc-text">{markdown(item.content)}</div>'
    if isinstance(item, ui.Separator):
        return '<hr class="dc-sep">' if item.visible else '<div class="dc-gap"></div>'
    if isinstance(item, ui.Section):
        body = ''.join(_item(c, files) for c in item.children)
        acc = item.accessory
        side = ''
        if isinstance(acc, ui.Thumbnail):
            side = f'<img class="dc-thumb" src="{html.escape(_media(acc.media.url, files))}" alt="" width="80" height="80">'
        elif isinstance(acc, ui.Button):
            side = _button(acc)
        return f'<div class="dc-section"><div class="dc-section-body">{body}</div>{side}</div>'
    if isinstance(item, ui.ActionRow):
        return '<div class="dc-row">' + ''.join(_button(b) for b in item.children if isinstance(b, ui.Button)) + '</div>'
    if isinstance(item, ui.MediaGallery):
        return '<div class="dc-gallery">' + ''.join(
            f'<img src="{html.escape(_media(m.media.url, files))}" alt="">' for m in item.items) + '</div>'
    if isinstance(item, ui.Container):
        accent = f' style="--accent:#{item.accent_colour.value:06x}"' if item.accent_colour else ''
        return f'<div class="dc-container"{accent}>' + ''.join(_item(c, files) for c in item.children) + '</div>'
    return ''


def render_view(view: ui.LayoutView) -> str:
    files = attached(view)
    return ''.join(_item(c, files) for c in view.children)
