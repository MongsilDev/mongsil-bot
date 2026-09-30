"""
카드 색과 차트 이미지. 명령어 카드와 웹 미리보기 공용
"""
import io
import math
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import discord
from discord import ui
from PIL import Image, ImageDraw, ImageFont

KST = timezone(timedelta(hours=9))

# 티어 아이콘 번호별 카드 색. 아이콘의 주조색을 어두운 배경에서 보이게 밝힘
TIER_COLOURS = {
    '0': 0x6B7280, '1': 0x8A9199, '2': 0xC0773A, '3': 0x8EA7C4, '4': 0xE0B356,
    '5': 0x4FC2B4, '6': 0xA78BDB, '7': 0x8C9BE8, '8': 0x5BB6E0, '9': 0xD9A24A, '10': 0xE84C7D,
}

# 명령어별 고정 색
COLOURS = {
    'concurrent': 0x3BA55C,
    'season': 0xF0B232,
    'cut': 0xE84C7D,
    'playtime': 0x5865F2,
    'ranking': 0xD9A24A,
    'info': 0x5865F2,
    'animal': 0xF47B67,
}

# 차트 배경은 디스코드 다크 모드 카드색. 라이트 모드에서도 한 패널로 보임
BG = (43, 45, 49)
GRID = (63, 65, 71)
TEXT = (219, 222, 225)
SUBTEXT = (148, 155, 164)

FONT_FILES = [
    ('/usr/share/fonts/opentype/noto/NotoSansCJK-{w}.ttc', 1),
    ('data/NanumGothic.ttf', 0),
]
_fonts: Dict[Tuple[int, bool], ImageFont.FreeTypeFont] = {}

# 2배로 그린 뒤 줄여 계단 현상 제거
SCALE = 2


def colour(name: str) -> discord.Colour:
    return discord.Colour(COLOURS[name])


def tier_colour(icon: str) -> discord.Colour:
    return discord.Colour(TIER_COLOURS.get(str(icon), TIER_COLOURS['0']))


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    if key not in _fonts:
        for path, index in FONT_FILES:
            try:
                _fonts[key] = ImageFont.truetype(path.format(w='Bold' if bold else 'Regular'), size * SCALE, index=index)
                break
            except OSError:
                continue
        else:
            _fonts[key] = ImageFont.load_default(size * SCALE)
    return _fonts[key]


def attach(view: ui.LayoutView, name: str, data: bytes) -> str:
    """뷰에 이미지 파일을 붙이고 컴포넌트용 attachment 주소 반환"""
    files = view.__dict__.setdefault('_card_files', {})
    files[name] = data
    return f'attachment://{name}'


def attached(view: ui.LayoutView) -> Dict[str, bytes]:
    return view.__dict__.get('_card_files', {})


def files_of(view: ui.LayoutView) -> List[discord.File]:
    return [discord.File(io.BytesIO(data), filename=name) for name, data in attached(view).items()]


def gauge(ratio: float, width: int = 12) -> str:
    """텍스트 게이지. 0이 아니면 최소 한 칸"""
    ratio = min(max(ratio, 0.0), 1.0)
    filled = max(1, round(ratio * width)) if ratio > 0 else 0
    return '▰' * filled + '▱' * (width - filled)


def _canvas(w: int, h: int) -> Tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new('RGB', (w * SCALE, h * SCALE), BG)
    return img, ImageDraw.Draw(img)


def _png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.resize((img.width // SCALE, img.height // SCALE), Image.LANCZOS).save(out, 'PNG', optimize=True)
    return out.getvalue()


def _hex(value: int) -> Tuple[int, int, int]:
    return (value >> 16) & 255, (value >> 8) & 255, value & 255


def _mix(a: Tuple[int, int, int], b: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _ticks(lo: float, hi: float, count: int = 5) -> List[float]:
    raw = max(hi - lo, 1) / count
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks = [start]
    while ticks[-1] < hi:
        ticks.append(ticks[-1] + step)
    return ticks


def line_chart(points: Sequence[Tuple[datetime, float]], accent: int, *, hours: int = 24,
               mark_extremes: bool = True, width: int = 720, height: int = 240) -> Optional[bytes]:
    """시간 축 선 그래프. 아래 채움, 최고와 최저 점 표시"""
    return lines_chart([(points, accent)], hours=hours, mark_extremes=mark_extremes, fill=True,
                       width=width, height=height)


def lines_chart(series: Sequence[Tuple[Sequence[Tuple[datetime, float]], int]], *, hours: int = 24,
                mark_extremes: bool = False, fill: bool = False, end_labels: bool = False,
                legend: Sequence[str] = (), width: int = 720, height: int = 240) -> Optional[bytes]:
    colours = [accent for _, accent in series]
    series = [([(t, v) for t, v in pts if v is not None], accent) for pts, accent in series]
    series = [(pts, accent) for pts, accent in series if len(pts) >= 2]
    if not series:
        return None
    img, d = _canvas(width, height)
    s = SCALE
    left, right, top, bottom = 56 * s, (width - (64 if end_labels else 16)) * s, (32 if legend else 16) * s, (height - 30) * s
    everything = [p for pts, _ in series for p in pts]
    t0, t1 = min(t for t, _ in everything), max(t for t, _ in everything)
    span = max((t1 - t0).total_seconds(), 1)
    lo, hi = min(v for _, v in everything), max(v for _, v in everything)
    ticks = _ticks(max(lo - (hi - lo) * 0.08, 0) if lo >= 0 else lo, hi + (hi - lo) * 0.08)
    lo, hi = ticks[0], ticks[-1]

    def xy(t: datetime, v: float) -> Tuple[float, float]:
        return (left + (t - t0).total_seconds() / span * (right - left),
                bottom - (v - lo) / (hi - lo) * (bottom - top))

    small = font(11)
    for v in ticks:
        y = bottom - (v - lo) / (hi - lo) * (bottom - top)
        d.line([(left, y), (right, y)], fill=GRID, width=s)
        d.text((left - 8 * s, y), f'{v:,.0f}', font=small, fill=SUBTEXT, anchor='rm')
    tick = (t0.astimezone(KST) + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    while tick <= t1:
        if hours <= 48 and tick.hour % 6 == 0:
            d.text((xy(tick, lo)[0], bottom + 8 * s), f'{tick.hour}시', font=small, fill=SUBTEXT, anchor='mt')
        elif hours > 48 and tick.hour == 0:
            d.text((xy(tick, lo)[0], bottom + 8 * s), f'{tick.month}/{tick.day}', font=small, fill=SUBTEXT, anchor='mt')
        tick += timedelta(hours=1)

    for pts, accent in series:
        line = [xy(t, v) for t, v in pts]
        rgb = _hex(accent)
        if fill:
            layer = Image.new('RGB', img.size, _mix(BG, rgb, 0.28))
            mask = Image.new('L', img.size, 0)
            ImageDraw.Draw(mask).polygon(line + [(line[-1][0], bottom), (line[0][0], bottom)], fill=255)
            fade = Image.linear_gradient('L').resize(img.size)
            mask = Image.composite(mask, Image.new('L', img.size, 0), fade.point(lambda p: 255 - p))
            img.paste(layer, (0, 0), mask)
        d.line(line, fill=rgb, width=3 * s, joint='curve')

        if mark_extremes:
            hi_pt = max(range(len(pts)), key=lambda i: pts[i][1])
            lo_pt = min(range(len(pts)), key=lambda i: pts[i][1] if pts[i][1] > 0 else float('inf'))
            for i, col in ((hi_pt, rgb), (lo_pt, SUBTEXT)):
                x, y = line[i]
                d.ellipse([x - 5 * s, y - 5 * s, x + 5 * s, y + 5 * s], fill=col, outline=BG, width=2 * s)
            x, y = line[hi_pt]
            x = min(max(x, left + 30 * s), right - 30 * s)
            d.text((x, y - 10 * s), f'{pts[hi_pt][1]:,.0f}', font=font(12, bold=True), fill=TEXT, anchor='mb')
        x, y = line[-1]
        d.ellipse([x - 6 * s, y - 6 * s, x + 6 * s, y + 6 * s], fill=(255, 255, 255), outline=rgb, width=3 * s)
        if end_labels:
            d.text((x + 10 * s, y), f'{pts[-1][1]:,.0f}', font=font(12, bold=True), fill=rgb, anchor='lm')
    x = left + 10 * s
    for label, accent in zip(legend, colours):
        d.ellipse([x, 8 * s, x + 10 * s, 18 * s], fill=_hex(accent))
        d.text((x + 16 * s, 13 * s), label, font=font(12, bold=True), fill=TEXT, anchor='lm')
        x += (30 + len(label) * 13) * s
    return _png(img)


def heatmap(grid: Sequence[Sequence[float]], row_labels: Sequence[str], accent: int,
            right_labels: Optional[Sequence[str]] = None, width: int = 720) -> bytes:
    """행 x 24시간 히트맵. 칸 색이 진할수록 큰 값"""
    rows, cols = len(grid), len(grid[0])
    cell, gap = 22, 4
    left, top, right = 64, 6, 96 if right_labels else 12
    height = top + rows * (cell + gap) + 24
    img, d = _canvas(width, height)
    s = SCALE
    cell_w = (width - left - right) / cols
    peak = max((max(r) for r in grid), default=0) or 1
    rgb = _hex(accent)
    empty = _mix(BG, (255, 255, 255), 0.05)
    small, bold = font(12), font(12, bold=True)
    for r in range(rows):
        y = (top + r * (cell + gap)) * s
        mid = y + cell * s / 2
        d.text((8 * s, mid), row_labels[r], font=small, fill=SUBTEXT, anchor='lm')
        for c in range(cols):
            x = (left + c * cell_w) * s
            v = grid[r][c]
            col = _mix(_mix(BG, rgb, 0.3), rgb, (v / peak) ** 0.6) if v > 0 else empty
            d.rounded_rectangle([x, y, x + (cell_w - gap) * s, y + cell * s], radius=4 * s, fill=col)
        if right_labels:
            d.text(((width - 8) * s, mid), right_labels[r], font=bold,
                   fill=TEXT if right_labels[r] != '-' else SUBTEXT, anchor='rm')
    for c in range(0, cols + 1, 6):
        x = (left + c * cell_w - (gap / 2 if c else 0)) * s
        d.text((x, (top + rows * (cell + gap) + 4) * s), f'{c}시', font=small, fill=SUBTEXT,
               anchor='ma' if c else 'la')
    return _png(img)


PLACE_COLOURS = [(1, (240, 178, 50)), (3, (79, 194, 180)), (6, (138, 145, 153)), (99, (229, 72, 77))]


def place_colour(place: int) -> Tuple[int, int, int]:
    return next(col for limit, col in PLACE_COLOURS if place <= limit)


def rp_chart(start_rp: int, games: Sequence[Tuple[int, int]], accent: int,
             width: int = 720, height: int = 210) -> Optional[bytes]:
    """최근 게임 RP 흐름. games는 오래된 순 (게임 후 RP, 순위), 아래 줄에 순위 칩"""
    if not games:
        return None
    img, d = _canvas(width, height)
    s = SCALE
    values = [start_rp] + [rp for rp, _ in games]
    pad = max((max(values) - min(values)) * 0.18, 20)
    ticks = _ticks(min(values) - pad, max(values) + pad, 4)
    lo, hi = ticks[0], ticks[-1]
    left, right, top, bottom = 56, width - 64, 14, height - 52
    n = len(values)

    def xy(i: int, v: float) -> Tuple[float, float]:
        return ((left + i / (n - 1) * (right - left)) * s,
                (bottom - (v - lo) / (hi - lo) * (bottom - top)) * s)

    small = font(11)
    for v in ticks:
        _, y = xy(0, v)
        d.line([(left * s, y), (right * s, y)], fill=GRID, width=s)
        d.text(((left - 8) * s, y), f'{v:,.0f}', font=small, fill=SUBTEXT, anchor='rm')

    rgb = _hex(accent)
    line = [xy(i, v) for i, v in enumerate(values)]
    d.line(line, fill=rgb, width=3 * s, joint='curve')
    chip = font(11, bold=True)
    radius = min(11, (right - left) / n / 2 - 1)
    for i, (_, place) in enumerate(games, start=1):
        x, y = line[i]
        col = place_colour(place)
        d.ellipse([x - 5 * s, y - 5 * s, x + 5 * s, y + 5 * s], fill=col, outline=BG, width=2 * s)
        cy = (height - 22) * s
        d.rounded_rectangle([x - radius * s, cy - radius * s, x + radius * s, cy + radius * s], radius=4 * s, fill=col)
        d.text((x, cy), str(place), font=chip, fill=BG, anchor='mm')
    x, y = line[0]
    d.ellipse([x - 4 * s, y - 4 * s, x + 4 * s, y + 4 * s], fill=SUBTEXT)
    x, y = line[-1]
    d.text((x + 10 * s, y), f'{values[-1]:,}', font=font(12, bold=True), fill=TEXT, anchor='lm')
    top_i = max(range(n), key=lambda i: values[i])
    low_i = min(range(n), key=lambda i: values[i])
    label = font(11, bold=True)
    for i, above in ((top_i, True), (low_i, False)):
        if i == n - 1 or values[top_i] == values[low_i]:
            continue
        x, y = line[i]
        x = min(max(x, (left + 20) * s), (right - 20) * s)
        d.text((x, y - 9 * s if above else y + 9 * s), f'{values[i]:,}', font=label,
               fill=TEXT if above else SUBTEXT, anchor='mb' if above else 'mt')
    return _png(img)
