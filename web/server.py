"""
웹 대시보드. 봇 프로세스 안에서 aiohttp로 실행돼 봇 상태와 설정 캐시를 그대로 공유
"""
import asyncio
import hashlib
import json
import os
import re
import secrets
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
import discord
import jinja2
import markupsafe
from aiohttp import web

from commands.alerts import channel_ok, hour_label
from utils import accounts, guild_settings
from utils.config import config
from utils.errors import BotError
from utils.emoji_zoom import load_disabled_servers, save_disabled_servers, schedule_webhook_removal
from utils.logging_config import get_logger
from utils.usage_db import db
from web import previews
from web.discord_render import images as preview_images

logger = get_logger('대시보드')

API = 'https://discord.com/api/v10'
ROOT = Path(__file__).parent
KST = timezone(timedelta(hours=9))
SERVICE_START = datetime(2023, 6, 15, tzinfo=KST)

ADMINISTRATOR = 1 << 3
MANAGE_GUILD = 1 << 5

# HTTPS에서는 __Host- 접두사로 하위 도메인이 쿠키를 설정하지 못하게 함. 이름은 start_dashboard에서 정함
SESSION_COOKIE = 'mb_session'
STATE_COOKIE = 'mb_state'
# 같은 IP에서 오는 OAuth 토큰 교환은 이 간격에 한 번만 보냄
CALLBACK_GAP = 5.0
_last_callback: dict[str, float] = {}
SESSION_TTL = 7 * 86400
GUILDS_TTL = 60
GUILD_INFO_TTL = 300
# 이 시간 안의 캐시는 바로 쓰고 뒤에서 새로 받음
STALE_TTL = 600
# 디스코드 API 오류 때 화면에 대신 쓰는 캐시의 최대 나이. 설정 저장은 따로 권한을 다시 확인함
FALLBACK_TTL = 3600


# 봇에서 자주 쓰는 순서, 목록에 없는 명령은 맨 뒤
COMMAND_ORDER = ['랭크', '랭킹', '이터컷', '플탐', '시즌', '동접', '대회', '계정', '강아지', '고양이', '설정', '정보']

# 이모지 확대와 명령어는 스레드와 포럼 글도 부모 채널 기준
SCOPE_TYPES = (discord.TextChannel, discord.VoiceChannel, discord.ForumChannel)
SCOPES = {
    'zoom_scope': ('zoom_mode', 'zoom_channels', ('all', 'only', 'except')),
    'command_scope': ('command_mode', 'command_channels', ('all', 'only')),
}
INVITE_DAYS = (0, 1, 3, 7, 30)
SOURCES = {'dashboard': '대시보드', 'discord': '디스코드', 'bot': '자동'}

client: discord.Client = None
base_url = ''
client_secret = ''
http: aiohttp.ClientSession = None
guild_cache: dict[int, tuple[float, list]] = {}
guild_info_cache: dict[int, tuple[float, dict]] = {}
refreshing: set = set()
background: set = set()

env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(ROOT / 'templates'),
    autoescape=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


def number(n) -> str:
    return f"{n:,}"


def compact(n) -> str:
    if n >= 10000:
        return f"{n / 10000:.1f}".rstrip('0').rstrip('.') + '만'
    return f"{n:,}"


def initials(name: str) -> str:
    words = name.split()
    return ''.join(w[0] for w in words[:2]) if len(words) > 1 else name[:2]


def duration(td: timedelta | None) -> str:
    if td is None:
        return '-'
    minutes = int(td.total_seconds() // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}일 {hours}시간"
    if hours:
        return f"{hours}시간 {minutes}분"
    return f"{minutes}분"


env.filters['number'] = number
env.filters['compact'] = compact
env.filters['initials'] = initials
env.filters['duration'] = duration
env.filters['date'] = lambda d: f"{d.year}년 {d.month}월 {d.day}일"
env.filters['numdate'] = lambda d: d.strftime('%Y.%m.%d')
env.filters['mdate'] = lambda d: f"{d.month}월 {d.day}일" if d.year == datetime.now(KST).year else f"{d.year}년 {d.month}월 {d.day}일"
def shortdate(d: datetime) -> str:
    """기록은 1년 보관이라 올해가 아니면 연도를 붙임"""
    year = f"{d.year}년 " if d.year != datetime.now(KST).year else ''
    return f"{year}{d.month}월 {d.day}일 {d:%H:%M}"


env.filters['shortdate'] = shortdate
env.filters['lines'] = lambda text: markupsafe.Markup('<br>').join(re.split(r'(?<=[.?!])\s+', text))
env.globals['session_avatar'] = lambda s: avatar_url(s['user_id'], s['avatar'])
env.globals['support_server'] = '/support'
env.globals['bot_avatar'] = lambda size: client.user.display_avatar.with_size(size).url if client.user else '/favicon.ico'
env.globals['static_version'] = str(int(max(f.stat().st_mtime for f in (ROOT / 'static').iterdir())))


def render(request: web.Request, name: str, status: int = 200, **context) -> web.Response:
    session = request.get('session')
    public = {k: session[k] for k in ('user_id', 'username', 'avatar', 'csrf')} if session else None
    html = env.get_template(name).render(
        session=public,
        is_owner=bool(session) and session['user_id'] in owner_ids(),
        path=request.path,
        rail=context.pop('rail', None),
        **context,
    )
    return web.Response(text=html, status=status, content_type='text/html')


def error_page(request: web.Request, status: int, title: str, message: str) -> web.Response:
    return render(request, 'error.html', status=status, title=title, message=message, code=status)


def owner_ids() -> set[int]:
    app = client.application
    if app is None:
        return set()
    if app.team:
        return {m.id for m in app.team.members}
    return {app.owner.id}


def avatar_url(user_id: int, avatar) -> str:
    if avatar:
        return f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png?size=64"
    return f"https://cdn.discordapp.com/embed/avatars/{(user_id >> 22) % 6}.png"


def guild_icon(guild_id: int, icon) -> str | None:
    if not icon:
        return None
    return f"https://cdn.discordapp.com/icons/{guild_id}/{icon}.png?size=96"


def invite_url(guild_id: int | None = None) -> str:
    app = client.application
    params = {'client_id': client.application_id, 'scope': 'bot applications.commands'}
    if app and app.install_params:
        params['permissions'] = app.install_params.permissions.value
    if guild_id:
        params['guild_id'] = guild_id
        params['disable_guild_select'] = 'true'
    return 'https://discord.com/oauth2/authorize?' + urlencode(params)


def kst_day(ts: int) -> str:
    return datetime.fromtimestamp(ts, KST).strftime('%Y-%m-%d')


def daily_series(rows, days: int = 30) -> dict:
    """ts 열이 있는 행을 KST 날짜별 막대 차트 데이터로 변환"""
    counts = Counter(kst_day(r['ts']) for r in rows)
    today = datetime.now(KST).date()
    series = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        series.append({'key': d.isoformat(), 'label': f"{d.month}월 {d.day}일", 'short': f"{d.month}.{d.day}",
                       'value': counts.get(d.isoformat(), 0)})
    peak = max(s['value'] for s in series)
    # 눈금은 1, 2, 4, 5, 6, 8에 10의 거듭제곱을 곱한 값 중 최댓값 이상인 가장 작은 수
    top, scale = 1, 1
    while top < peak:
        for step in (1, 2, 4, 5, 6, 8, 10):
            top = step * scale
            if top >= peak:
                break
        else:
            scale *= 10
            continue
        break
    for s in series:
        s['pct'] = round(s['value'] / top * 100, 2) if top else 0
    return {'series': series, 'top': top, 'mid': top // 2 if top % 2 == 0 else None, 'total': sum(counts.values())}


def day_start(days_ago: int = 0) -> int:
    d = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)
    return int(d.timestamp())


def delta(cur: int, prev: int, comparable: bool = True) -> dict | None:
    """지난 기간 대비 증감. 지난 기간 기록이 없으면 비교하지 않음"""
    if not comparable or not prev:
        return None
    change = (cur - prev) / prev * 100
    if abs(change) < 0.5:
        return {'text': '변화 없음', 'dir': 'flat'}
    return {'text': f"{change:+.0f}%", 'dir': 'up' if change > 0 else 'down'}


def first_ts(table: str) -> int | None:
    return db.execute(f"SELECT MIN(ts) FROM {table}").fetchone()[0]


def tally(names, limit: int | None = None) -> list[dict]:
    counter = Counter(names)
    counts = counter.most_common(limit)
    total = sum(counter.values()) or 1
    top = counts[0][1] if counts else 1
    return [{'name': n, 'count': c, 'bar': round(c / top * 100, 1), 'share': c / total * 100} for n, c in counts]


def percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))]


def usage_summary(cmd_rows, zoom_rows, now: int, start: int | None = None) -> dict:
    """30일 사용량 타일과 차트. 행은 60일치를 받아 앞 30일과 비교. start는 그 서버에 몽실봇이 추가된 시각"""
    # 차트 막대와 같은 범위가 되게 29일 전 0시부터
    since, prev = day_start(29), day_start(59)
    cur = [r for r in cmd_rows if r['ts'] >= since]
    old = [r for r in cmd_rows if prev <= r['ts'] < since]
    zcur = [r for r in zoom_rows if r['ts'] >= since]
    zold = [r for r in zoom_rows if prev <= r['ts'] < since]
    cmd_first, zoom_first = first_ts('command_log'), first_ts('zoom_log')
    if start:
        cmd_first, zoom_first = max(cmd_first or start, start), max(zoom_first or start, start)
    cmd_full = bool(cmd_first) and cmd_first <= prev
    zoom_full = bool(zoom_first) and zoom_first <= prev
    today, yesterday = day_start(), day_start(1)
    users, old_users = {r['user_id'] for r in cur}, {r['user_id'] for r in old}
    # 30일이 다 차지 않았을 때만 기록 시작일을 붙임. 다 찼는데 비교만 못 하면 날짜는 의미 없음
    cmd_since = datetime.fromtimestamp(cmd_first, KST) if cmd_first and cmd_first > since else None
    zoom_since = datetime.fromtimestamp(zoom_first or now, KST) if not zoom_first or zoom_first > since else None
    return {
        'commands': {'value': len(cur), 'delta': delta(len(cur), len(old), cmd_full), 'since': cmd_since},
        'users': {'value': len(users), 'delta': delta(len(users), len(old_users), cmd_full), 'since': cmd_since},
        'zooms': {'value': len(zcur), 'delta': delta(len(zcur), len(zold), zoom_full), 'since': zoom_since},
        'today': {'value': sum(1 for r in cur if r['ts'] >= today),
                  'yesterday': sum(1 for r in cur if yesterday <= r['ts'] < today),
                  'same_time': sum(1 for r in cur if yesterday <= r['ts'] < now - 86400)},
        'cmd_chart': daily_series(cur),
        'zoom_chart': daily_series(zcur),
        'cmd_since': cmd_since,
        'zoom_since': zoom_since,
        'rows': cur,
        'zoom_rows': zcur,
    }


# ---------- 세션 ----------

def session_key(sid: str) -> str:
    """DB에는 세션 값의 해시만 저장"""
    return hashlib.sha256(sid.encode()).hexdigest()


def same_token(a: str, b: str) -> bool:
    """token_urlsafe 값 비교. 깨진 쿠키처럼 ASCII가 아닌 입력은 다른 값"""
    return isinstance(a, str) and a.isascii() and b.isascii() and secrets.compare_digest(a, b)


def secure_headers(request: web.Request, response) -> None:
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('Referrer-Policy', 'same-origin')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    # 정적 경로의 오류 응답은 로그인 정보가 담긴 HTML이라 캐시 대상 아님
    if not request.path.startswith(('/static/', '/preview/')) or response.status >= 400:
        # no-store는 뒤로 가기 캐시를 비활성화해 no-cache로 둠
        response.headers.setdefault('Cache-Control', 'private, no-cache')
    if response.content_type == 'text/html':
        response.headers.setdefault('Speculation-Rules', '"/speculation.json"')
    response.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; img-src 'self' https: data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' https://www.googletagmanager.com https://static.cloudflareinsights.com; "
        "connect-src 'self' https://*.google-analytics.com https://*.analytics.google.com "
        "https://*.googletagmanager.com https://cloudflareinsights.com; "
        "frame-ancestors 'none'; form-action 'self'",
    )


@web.middleware
async def session_middleware(request: web.Request, handler):
    sid = request.cookies.get(SESSION_COOKIE)
    request['session'] = None
    if sid and sid.isascii():
        row = db.execute("SELECT * FROM sessions WHERE id = ? AND expires > ?", (session_key(sid), int(time.time()))).fetchone()
        if row:
            request['session'] = dict(row)
    started = time.perf_counter()
    try:
        response = await handler(request)
    except web.HTTPException as e:
        secure_headers(request, e)
        raise
    secure_headers(request, response)
    # 브라우저 개발자 도구에서 서버 처리 시간과 네트워크 시간을 나눠 보려고 붙임
    response.headers['Server-Timing'] = f"app;dur={(time.perf_counter() - started) * 1000:.1f}"
    return response


@web.middleware
async def ready_middleware(request: web.Request, handler):
    """재시작 직후 서버 목록이 비어 있는 동안 틀린 화면 대신 잠시 기다리게 함"""
    if client.is_ready() or request.path.startswith(('/static/', '/health')):
        return await handler(request)
    response = error_page(request, 503, "몽실봇을 다시 시작하는 중", "몇 초 뒤 새로고침해주세요.")
    response.headers['Retry-After'] = '5'
    return response


def wants_json(request: web.Request) -> bool:
    """설정 저장은 app.js의 fetch라 오류도 JSON으로 돌려줘야 화면에 원인이 표시됨"""
    return request.method == 'POST' and 'X-CSRF-Token' in request.headers


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPUnauthorized:
        if wants_json(request):
            return web.json_response({'error': "로그인이 만료됐습니다. 새로고침한 뒤 다시 로그인해주세요."}, status=401)
        raise web.HTTPFound('/login?' + urlencode({'next': request.path}))
    except web.HTTPForbidden as e:
        if e.text == 'CSRF':
            title, message = "페이지가 오래됐습니다", "새로고침한 뒤 다시 시도해주세요."
            detail = f"{title}. {message}"
        else:
            title, message = "권한이 없습니다", "이 서버를 관리할 권한이 없습니다. 서버 관리 권한을 확인해주세요."
            detail = message
        if wants_json(request):
            return web.json_response({'error': detail}, status=403)
        return error_page(request, 403, title, message)
    except web.HTTPBadRequest:
        if wants_json(request):
            return web.json_response({'error': "잘못된 요청입니다. 새로고침한 뒤 다시 시도해주세요."}, status=400)
        return error_page(request, 400, "잘못된 요청입니다", "주소를 확인해주세요.")
    except web.HTTPNotFound:
        if wants_json(request):
            return web.json_response({'error': "서버를 찾을 수 없습니다. 새로고침한 뒤 다시 시도해주세요."}, status=404)
        return error_page(request, 404, "페이지를 찾을 수 없습니다", "주소를 확인해주세요.")
    except web.HTTPException:
        raise
    except Exception:
        logger.error(f"대시보드 처리 오류: {request.method} {request.path}", exc_info=True)
        return error_page(request, 500, "오류가 발생했습니다", "잠시 후 다시 시도해주세요.")


def cookie_opts() -> dict:
    return {'httponly': True, 'secure': base_url.startswith('https'), 'samesite': 'Lax', 'path': '/'}


def require_login(request: web.Request):
    if not request['session']:
        # POST는 error_middleware가 JSON 안내로, GET은 로그인으로 보냄
        raise web.HTTPUnauthorized()
    return request['session']


def check_csrf(request: web.Request, token: str | None):
    session = request['session']
    if not session or not token or not same_token(token, session['csrf']):
        raise web.HTTPForbidden(text='CSRF')


class SessionExpired(Exception):
    pass


async def fetch_guilds(session: dict) -> list[dict]:
    async with http.get(f"{API}/users/@me/guilds", headers={'Authorization': f"Bearer {session['token']}"}) as r:
        if r.status == 401:
            db.execute("DELETE FROM sessions WHERE id = ?", (session['id'],))
            guild_cache.pop(session['user_id'], None)
            raise SessionExpired()
        r.raise_for_status()
        guilds = await r.json()
    now = time.time()
    for key in [k for k, (ts, _) in guild_cache.items() if now - ts > FALLBACK_TTL]:
        del guild_cache[key]
    guild_cache[session['user_id']] = (now, guilds)
    return guilds


def refresh_later(key, coro_fn) -> None:
    """같은 대상을 한 번에 하나만 뒤에서 새로 받음"""
    if key in refreshing:
        return

    async def run():
        try:
            await coro_fn()
        except Exception as e:
            logger.warning(f"대시보드 캐시 갱신 실패: {key[0]} {type(e).__name__}: {e}")
        finally:
            refreshing.discard(key)
    refreshing.add(key)
    task = asyncio.create_task(run())
    background.add(task)
    task.add_done_callback(background.discard)


async def user_guilds(request: web.Request) -> list[dict]:
    """로그인 유저의 서버 목록. 60초가 지나면 캐시를 쓰면서 뒤에서 새로 받음"""
    session = request['session']
    cached = guild_cache.get(session['user_id'])
    age = time.time() - cached[0] if cached else None
    if cached and age < GUILDS_TTL:
        return cached[1]
    if cached and age < STALE_TTL:
        refresh_later(('guilds', session['user_id']), lambda: fetch_guilds(session))
        return cached[1]
    try:
        return await fetch_guilds(session)
    except SessionExpired:
        raise web.HTTPFound('/login?' + urlencode({'next': request.path}))
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        if cached and age < FALLBACK_TTL:
            logger.warning(f"서버 목록 갱신 실패, 캐시 사용: {type(e).__name__}")
            return cached[1]
        raise


def can_manage(g: dict) -> bool:
    perms = int(g.get('permissions', 0))
    return g.get('owner') or bool(perms & (ADMINISTRATOR | MANAGE_GUILD))


async def rail(request: web.Request) -> list[dict] | None:
    """왼쪽 서버 줄. 몽실봇이 있고 관리 권한이 있는 서버를 멤버 많은 순으로"""
    if not request['session']:
        return None
    items = []
    for g in await user_guilds(request):
        gid = int(g['id'])
        guild = client.get_guild(gid)
        if can_manage(g) and guild:
            items.append({'id': gid, 'name': g['name'], 'icon': guild_icon(gid, g.get('icon')), 'members': guild.member_count or 0})
    items.sort(key=lambda g: -g['members'])
    return items


# ---------- 로그인 ----------

async def login(request: web.Request):
    state = secrets.token_urlsafe(24)
    nxt = request.query.get('next', '/servers')
    # 역슬래시나 탭이 섞인 경로는 브라우저가 외부 주소로 해석해 내부 경로만 허용
    if not re.fullmatch(r'/(servers(/\d+)?|admin)?', nxt):
        nxt = '/servers'
    params = {
        'client_id': client.application_id,
        'response_type': 'code',
        'redirect_uri': f"{base_url}/auth/callback",
        'scope': 'identify guilds',
        'state': state,
    }
    response = web.HTTPFound('https://discord.com/oauth2/authorize?' + urlencode(params))
    response.set_cookie(STATE_COOKIE, f"{state}|{nxt}", max_age=600, **cookie_opts())
    raise response


async def callback(request: web.Request):
    saved = request.cookies.get(STATE_COOKIE, '')
    state, _, nxt = saved.partition('|')
    if 'error' in request.query:
        raise web.HTTPFound('/')
    if not state or not same_token(state, request.query.get('state', '')) or 'code' not in request.query:
        return error_page(request, 400, "로그인하지 못했습니다", "처음 화면에서 다시 로그인해주세요.")
    ip = request.headers.get('CF-Connecting-IP') or request.remote or ''
    now = time.monotonic()
    if now - _last_callback.get(ip, 0) < CALLBACK_GAP:
        return error_page(request, 429, "로그인하지 못했습니다", "잠시 후 처음 화면에서 다시 로그인해주세요.")
    _last_callback[ip] = now
    if len(_last_callback) > 5000:
        _last_callback.clear()

    data = {
        'grant_type': 'authorization_code',
        'code': request.query['code'],
        'redirect_uri': f"{base_url}/auth/callback",
    }
    auth = aiohttp.BasicAuth(str(client.application_id), client_secret)
    async with http.post(f"{API}/oauth2/token", data=data, auth=auth) as r:
        if r.status != 200:
            logger.warning(f"OAuth 토큰 교환 실패: {r.status} {await r.text()}")
            return error_page(request, 400, "로그인하지 못했습니다", "처음 화면에서 다시 로그인해주세요.")
        token = await r.json()

    async with http.get(f"{API}/users/@me", headers={'Authorization': f"Bearer {token['access_token']}"}) as r:
        r.raise_for_status()
        user = await r.json()

    if not re.fullmatch(r'/(servers(/\d+)?|admin)?', nxt):
        nxt = '/servers'
    sid = secrets.token_urlsafe(32)
    expires = int(time.time()) + min(SESSION_TTL, int(token.get('expires_in', SESSION_TTL)))
    db.execute("DELETE FROM sessions WHERE expires < ?", (int(time.time()),))
    db.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_key(sid), int(user['id']), user.get('global_name') or user['username'], user.get('avatar'),
         token['access_token'], secrets.token_urlsafe(24), expires),
    )
    logger.info(f"대시보드 로그인: {user['username']} ({user['id']})")

    response = web.HTTPFound(nxt or '/servers')
    response.set_cookie(SESSION_COOKIE, sid, max_age=expires - int(time.time()), **cookie_opts())
    response.del_cookie(STATE_COOKIE, **cookie_opts())
    raise response


async def logout(request: web.Request):
    if not request['session']:
        raise web.HTTPForbidden(text='CSRF')
    try:
        form = await request.post()
    except (ValueError, LookupError):
        raise web.HTTPBadRequest()
    check_csrf(request, form.get('csrf'))
    session = request['session']
    db.execute("DELETE FROM sessions WHERE id = ?", (session['id'],))
    guild_cache.pop(session['user_id'], None)
    try:
        auth = aiohttp.BasicAuth(str(client.application_id), client_secret)
        async with http.post(f"{API}/oauth2/token/revoke", data={'token': session['token'], 'token_type_hint': 'access_token'}, auth=auth) as r:
            if r.status != 200:
                logger.warning(f"로그인 토큰 철회 실패: {r.status}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f"로그인 토큰 철회 실패: {type(e).__name__}")
    response = web.HTTPFound('/')
    response.del_cookie(SESSION_COOKIE, **cookie_opts())
    raise response


# ---------- 페이지 ----------

async def index(request: web.Request):
    commands = []
    for cmd in client.tree.get_commands():
        if not isinstance(cmd, discord.app_commands.Command):
            continue
        commands.append({
            'name': cmd.name,
            'description': cmd.description,
            'params': [p.display_name for p in cmd.parameters],
        })
    commands.sort(key=lambda c: COMMAND_ORDER.index(c['name']) if c['name'] in COMMAND_ORDER else 99)

    live = dict(previews.live)
    now = datetime.now(KST)
    if live.get('season_end'):
        live['season_days'] = (live['season_end'].date() - now.date()).days
        span = (live['season_end'] - live['season_start']).total_seconds()
        live['season_pct'] = max(0, min(100, (now - live['season_start']).total_seconds() / span * 100)) if span else 0
    series = live.get('players_series') or []
    if len(series) > 1:
        hi, lo = max(series), min(series)
        live['spark'] = ' '.join(
            f"{i / (len(series) - 1) * 100:.1f},{30 - (v - lo) / ((hi - lo) or 1) * 28:.1f}" for i, v in enumerate(series))
    stamp = None
    if previews.updated_at:
        t = datetime.fromtimestamp(previews.updated_at, KST)
        day = '오늘' if t.date() == now.date() else '어제'
        stamp = f"{day} {'오전' if t.hour < 12 else '오후'} {t.hour % 12 or 12}:{t.minute:02d}"

    return render(
        request, 'index.html',
        avatar=client.user.display_avatar.with_size(128).url if client.user else None,
        guild_count=len(client.guilds),
        user_count=sum(g.member_count or 0 for g in client.guilds),
        days=(datetime.now(KST).date() - SERVICE_START.date()).days + 1,
        commands=commands,
        selected=commands[0]['name'] if commands else None,
        previews=previews.previews,
        live=live,
        stamp=stamp,
        invite='/invite',
    )


async def servers(request: web.Request):
    require_login(request)
    guilds = [g for g in await user_guilds(request) if can_manage(g)]
    since = day_start(29)
    usage = dict(db.execute(
        "SELECT guild_id, COUNT(*) FROM command_log WHERE ts >= ? GROUP BY guild_id", (since,)).fetchall())
    zooms = dict(db.execute(
        "SELECT guild_id, COUNT(*) FROM zoom_log WHERE ts >= ? GROUP BY guild_id", (since,)).fetchall())
    disabled = load_disabled_servers()
    joined, others = [], []
    for g in sorted(guilds, key=lambda g: g['name'].lower()):
        gid = int(g['id'])
        item = {'id': gid, 'name': g['name'], 'icon': guild_icon(gid, g.get('icon'))}
        guild = client.get_guild(gid)
        if guild:
            item.update(members=guild.member_count, usage=usage.get(gid, 0), zooms=zooms.get(gid, 0),
                        zoom_on=gid not in disabled)
            joined.append(item)
        else:
            item['invite'] = f'/invite?guild={gid}'
            others.append(item)
    # 멤버 많은 서버가 위로, 같으면 이름순
    joined.sort(key=lambda g: -(g['members'] or 0))
    return render(request, 'servers.html', joined=joined, others=others, rail=joined)


async def verify_manager(request: web.Request, guild: discord.Guild) -> None:
    """설정 저장 직전에 봇 토큰으로 권한을 다시 확인. 서버 목록 캐시가 늦게 갱신돼도 권한을 잃은 유저의 저장은 거부됨"""
    user_id = request['session']['user_id']
    if guild.owner_id == user_id:
        return
    try:
        member = await guild.fetch_member(user_id)
    except discord.NotFound:
        raise web.HTTPForbidden(text='권한 없음')
    perms = member.guild_permissions
    if not (perms.administrator or perms.manage_guild):
        raise web.HTTPForbidden(text='권한 없음')


async def managed_guild(request: web.Request) -> discord.Guild:
    require_login(request)
    try:
        gid = int(request.match_info['guild_id'])
    except ValueError:
        raise web.HTTPNotFound()
    if not any(int(g['id']) == gid and can_manage(g) for g in await user_guilds(request)):
        raise web.HTTPNotFound()
    guild = client.get_guild(gid)
    if guild is None:
        raise web.HTTPNotFound()
    return guild


async def fetch_counts(guild: discord.Guild) -> dict:
    async def online():
        full = await client.fetch_guild(guild.id, with_counts=True)
        return full.approximate_presence_count

    async def owner():
        member = guild.get_member(guild.owner_id) or await guild.fetch_member(guild.owner_id)
        return member.display_name

    async def registered():
        ids = [r[0] for r in accounts.all_accounts()]
        found = 0
        for i in range(0, len(ids), 100):
            found += len(await guild.query_members(user_ids=ids[i:i + 100], limit=100, cache=False))
        return found

    extra = {}
    results = await asyncio.gather(asyncio.wait_for(online(), 3), asyncio.wait_for(owner(), 3),
                                   asyncio.wait_for(registered(), 3), return_exceptions=True)
    for key, result in zip(('online', 'owner', 'registered'), results):
        if isinstance(result, Exception):
            logger.warning(f"서버 {key} 조회 실패: {guild.id} {type(result).__name__} {result}")
        else:
            extra[key] = result
    guild_info_cache[guild.id] = (time.time(), extra)
    return extra


async def guild_counts(guild: discord.Guild) -> dict:
    """온라인 수, 소유자 이름, /계정 등록 멤버 수. 게이트웨이 캐시에 없어 따로 받고 5분이 지나면 뒤에서 새로 받음"""
    cached = guild_info_cache.get(guild.id)
    if cached:
        if time.time() - cached[0] >= GUILD_INFO_TTL:
            refresh_later(('counts', guild.id), lambda: fetch_counts(guild))
        return cached[1]
    return await fetch_counts(guild)


def channel_kind(channel) -> str:
    if isinstance(channel, discord.ForumChannel):
        return 'forum'
    if isinstance(channel, discord.VoiceChannel):
        return 'voice'
    return 'news' if channel.is_news() else 'text'


def channel_groups(guild: discord.Guild, types) -> list[dict]:
    """카테고리별 채널. 몽실봇이 볼 수 없는 채널은 이름도 내보내지 않음"""
    groups = []
    for category, channels in guild.by_category():
        items = [{'id': c.id, 'name': c.name, 'kind': channel_kind(c)} for c in channels
                 if isinstance(c, types) and c.permissions_for(guild.me).view_channel]
        if items:
            groups.append({'name': category.name if category else None, 'channels': items})
    return groups


def channel_names(guild: discord.Guild, ids) -> str:
    names = [f"#{c.name}" for c in map(guild.get_channel, ids) if c]
    return ", ".join(names[:3]) + (f" 외 {len(names) - 3}개" if len(names) > 3 else "")


def change_entry(row) -> dict:
    who = row['user_name'] or '몽실봇'
    when = datetime.fromtimestamp(row['ts'], KST)
    return {'text': row['text'], 'meta': f"{who}, {SOURCES.get(row['source'], row['source'])}, {shortdate(when)}",
            'bot': row['source'] == 'bot'}


def alert_rows(guild: discord.Guild) -> list[dict]:
    rows = []
    for key, name, desc in guild_settings.ALERTS:
        cid = guild_settings.get(guild.id, key)
        channel = guild.get_channel(cid) if cid else None
        problem = None
        if guild.unavailable:
            pass
        elif cid and not isinstance(channel, discord.TextChannel):
            problem = "알림 채널이 삭제됐습니다. 다른 채널을 선택해주세요."
        elif channel and not channel.permissions_for(guild.me).view_channel:
            # 11월 16일부터 볼 수 없는 채널은 이름이 가려져 이름을 쓰지 않음
            problem = "몽실봇이 알림 채널을 볼 수 없습니다. 채널 권한을 준 뒤 새로고침해주세요."
        elif channel and (missing := channel_ok(channel, key == 'alert_cut')):
            problem = f"#{channel.name} 채널에 몽실봇의 {', '.join(missing)} 권한이 없습니다. 권한을 준 뒤 새로고침해주세요."
        # 삭제됐거나 볼 수 없는 채널은 고르기 목록에 없어 따로 표시
        missing = bool(cid) and not (isinstance(channel, discord.TextChannel) and channel.permissions_for(guild.me).view_channel)
        rows.append({'key': key, 'name': name, 'desc': desc, 'channel': cid, 'problem': problem, 'missing': missing})
    return rows


def zoom_blocked(guild: discord.Guild) -> list[str]:
    """서버 권한은 있는데 채널 권한 설정 때문에 이모지 확대가 안 되는 채널"""
    if not guild.me.guild_permissions.manage_webhooks:
        return []
    out = []
    for channel in guild.channels:
        if isinstance(channel, SCOPE_TYPES) and guild_settings.zoom_allowed(guild.id, channel.id):
            perms = channel.permissions_for(guild.me)
            if perms.view_channel and not perms.manage_webhooks:
                out.append(f"#{channel.name}")
    return out


async def server_detail(request: web.Request):
    guild = await managed_guild(request)
    extra = await guild_counts(guild)
    perms = guild.me.guild_permissions
    info = {
        'banner': guild.banner.with_size(1024).url if guild.banner else None,
        'online': extra.get('online'),
        'owner': extra.get('owner'),
        'registered': extra.get('registered'),
        'boost_tier': guild.premium_tier,
        'boosts': guild.premium_subscription_count or 0,
        # emoji_limit은 일반과 움직이는 이모지 각각의 한도
        'emojis': sum(1 for e in guild.emojis if not e.animated),
        'animated': sum(1 for e in guild.emojis if e.animated),
        'emoji_limit': guild.emoji_limit,
        'created': guild.created_at.astimezone(KST),
    }
    permissions = [
        ('채널 보기', perms.view_channel, '메시지 확인', True),
        ('웹후크 관리', perms.manage_webhooks, '확대 이미지 전송', True),
        ('메시지 관리', perms.manage_messages, '원본 메시지 삭제', False),
    ]
    now = int(time.time())
    cmd_rows = db.execute("SELECT ts, user_id, command FROM command_log WHERE guild_id = ? AND ts >= ?",
                          (guild.id, now - 60 * 86400)).fetchall()
    zoom_rows = db.execute("SELECT ts, user_id FROM zoom_log WHERE guild_id = ? AND ts >= ?",
                           (guild.id, now - 60 * 86400)).fetchall()
    joined = guild.me.joined_at if guild.me else None
    usage = usage_summary(cmd_rows, zoom_rows, now, int(joined.timestamp()) if joined else None)

    return render(
        request, 'server.html',
        rail=await rail(request),
        guild={'id': guild.id, 'name': guild.name, 'icon': guild_icon(guild.id, guild.icon and guild.icon.key),
               'members': guild.member_count,
               'joined_at': guild.me.joined_at.astimezone(KST) if guild.me and guild.me.joined_at else None},
        info=info,
        permissions=permissions,
        zoom_enabled=guild.id not in load_disabled_servers(),
        can_webhook=perms.manage_webhooks,
        usage=usage,
        by_command=tally(r['command'] for r in usage['rows']),
        scope_channels=channel_groups(guild, SCOPE_TYPES),
        alert_channels=channel_groups(guild, discord.TextChannel),
        zoom_mode=guild_settings.get(guild.id, 'zoom_mode'),
        zoom_channels=set(guild_settings.get(guild.id, 'zoom_channels')),
        zoom_blocked=zoom_blocked(guild),
        command_mode=guild_settings.get(guild.id, 'command_mode'),
        command_channels=set(guild_settings.get(guild.id, 'command_channels')),
        invite_days=guild_settings.get(guild.id, 'invite_days'),
        invite_options=INVITE_DAYS,
        can_invites=perms.manage_guild,
        alerts=alert_rows(guild),
        cut_hour=guild_settings.get(guild.id, 'cut_hour'),
        hours=[(h, hour_label(h)) for h in range(24)],
        changes=[change_entry(r) for r in guild_settings.history(guild.id)],
    )


async def toggle_zoom(request: web.Request):
    guild = await managed_guild(request)
    check_csrf(request, request.headers.get('X-CSRF-Token'))
    try:
        body = await request.json()
    except (ValueError, LookupError):
        raise web.HTTPBadRequest()
    if not isinstance(body, dict):
        raise web.HTTPBadRequest()
    await verify_manager(request, guild)
    enable = bool(body.get('enabled'))

    disabled = set(load_disabled_servers())
    if enable:
        if not guild.me.guild_permissions.manage_webhooks:
            return web.json_response(
                {'enabled': False,
                 'error': "몽실봇에 웹후크 관리 권한이 없어 켤 수 없습니다. 몽실봇 역할에 웹후크 관리 권한을 준 뒤 다시 시도해주세요."},
                status=409,
            )
        disabled.discard(guild.id)
    else:
        disabled.add(guild.id)

    if not save_disabled_servers(disabled):
        return web.json_response({'enabled': not enable, 'error': "저장하지 못했습니다. 잠시 후 다시 시도해주세요."}, status=500)
    if not enable:
        schedule_webhook_removal(guild)
    logger.info(f"대시보드 설정 변경: {guild.name} ({guild.id}) 이모지 확대 {'켬' if enable else '끔'}")
    entry = record_change(request, guild, f"이모지 확대: {'켬' if enable else '끔'}")
    return web.json_response({'enabled': enable, 'entry': entry})


def record_change(request: web.Request, guild: discord.Guild, text: str) -> dict:
    session = request['session']
    guild_settings.log(guild.id, text, session['user_id'], session['username'])
    return change_entry({'ts': int(time.time()), 'user_name': session['username'], 'source': 'dashboard', 'text': text})


async def save_setting(request: web.Request):
    guild = await managed_guild(request)
    check_csrf(request, request.headers.get('X-CSRF-Token'))
    try:
        body = await request.json()
    except (ValueError, LookupError):
        raise web.HTTPBadRequest()
    if not isinstance(body, dict):
        raise web.HTTPBadRequest()
    await verify_manager(request, guild)
    key, value = body.get('key'), body.get('value')

    def refuse(message: str, status: int = 409):
        return web.json_response({'error': message}, status=status)

    def as_int(v) -> int | None:
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    changes: list[tuple[str, object]] = []
    if key in SCOPES:
        if not isinstance(value, dict) or not isinstance(value.get('channels', []), list):
            raise web.HTTPBadRequest()
        mode_key, list_key, modes = SCOPES[key]
        mode = (value or {}).get('mode')
        valid = {c['id'] for g in channel_groups(guild, SCOPE_TYPES) for c in g['channels']}
        ids = sorted({i for i in map(as_int, (value or {}).get('channels') or []) if i in valid})
        if mode not in modes:
            raise web.HTTPBadRequest()
        if mode != 'all' and not ids:
            return refuse("채널을 하나 이상 선택해주세요.", 400)
        changes = [(mode_key, mode), (list_key, ids)]
        subject = '이모지 확대' if key == 'zoom_scope' else '명령어'
        names = channel_names(guild, ids)
        text = f"{subject} 채널: " + {'all': "모든 채널", 'only': f"{names}만", 'except': f"{names} 제외"}[mode]
    elif key == 'invite_days':
        days = as_int(value)
        if days not in INVITE_DAYS:
            raise web.HTTPBadRequest()
        if days and not guild.me.guild_permissions.manage_guild:
            return refuse("몽실봇에 서버 관리 권한이 없어 초대 링크를 정리할 수 없습니다. 권한을 준 뒤 다시 시도해주세요.")
        changes = [(key, days)]
        text = f"초대 링크 정리: {days}일" if days else "초대 링크 정리: 끔"
    elif key in guild_settings.ALERT_NAMES:
        cid = as_int(value)
        name = guild_settings.ALERT_NAMES[key]
        if cid is None:
            raise web.HTTPBadRequest()
        if cid:
            channel = guild.get_channel(cid)
            if not isinstance(channel, discord.TextChannel) or not channel.permissions_for(guild.me).view_channel:
                return refuse("선택할 수 없는 채널입니다. 새로고침한 뒤 다시 선택해주세요.", 400)
            if missing := channel_ok(channel, key == 'alert_cut'):
                return refuse(f"몽실봇에 #{channel.name} 채널의 {', '.join(missing)} 권한이 없습니다. 권한을 준 뒤 다시 선택해주세요.")
            text = f"{name} 알림: #{channel.name}"
        else:
            text = f"{name} 알림: 끔"
        changes = [(key, cid)]
    elif key == 'cut_hour':
        hour = as_int(value)
        if hour is None or not 0 <= hour <= 23:
            raise web.HTTPBadRequest()
        changes = [(key, hour)]
        text = f"매일 이터컷 시각: {hour_label(hour)}"
    else:
        raise web.HTTPBadRequest()

    if all(guild_settings.get(guild.id, k) == v for k, v in changes):
        return web.json_response({'ok': True})
    if not all(guild_settings.put(guild.id, k, v) for k, v in changes):
        return refuse("저장하지 못했습니다. 잠시 후 다시 시도해주세요.", 500)
    logger.info(f"대시보드 설정 변경: {guild.name} ({guild.id}) {text}")
    return web.json_response({'ok': True, 'entry': record_change(request, guild, text)})


async def admin(request: web.Request):
    session = require_login(request)
    if session['user_id'] not in owner_ids():
        raise web.HTTPNotFound()

    now = int(time.time())
    since = now - 30 * 86400
    cmd_rows = db.execute("SELECT ts, guild_id, user_id, command, status, ms, shown_ms FROM command_log WHERE ts >= ?",
                          (now - 60 * 86400,)).fetchall()
    zoom_rows = db.execute("SELECT ts, guild_id, user_id FROM zoom_log WHERE ts >= ?", (now - 60 * 86400,)).fetchall()
    usage = usage_summary(cmd_rows, zoom_rows, now)
    rows = usage['rows']

    errors = sum(1 for r in rows if r['status'] == 'error')
    down = sum(1 for r in rows if r['status'] == 'down')
    commands = []
    for item in tally(r['command'] for r in rows):
        mine = [r for r in rows if r['command'] == item['name']]
        shown = [r['shown_ms'] for r in mine if r['shown_ms'] is not None]
        # 화면 표시 시각은 10월부터 일부 명령만 기록돼 표본이 충분할 때만 씀
        source = shown if len(shown) >= 20 else [r['ms'] for r in mine if r['ms'] is not None]
        failed = sum(1 for r in mine if r['status'] == 'error')
        counted = sum(1 for r in mine if r['status'] != 'down')
        # 표본이 100개 안 되면 느린 5%가 상위 몇 개 값이라 표시하지 않음
        item.update(errors=failed, error_rate=failed / counted * 100 if counted else 0,
                    median=percentile(source, 0.5), p95=percentile(source, 0.95) if len(source) >= 100 else None)
        commands.append(item)

    by_guild = Counter(r['guild_id'] for r in rows if r['guild_id'])
    zoom_by_guild = Counter(r['guild_id'] for r in usage['zoom_rows'])
    top_guilds = []
    for gid, count in by_guild.most_common(10):
        g = client.get_guild(gid)
        if g is None:
            last = db.execute("SELECT name FROM guild_events WHERE guild_id = ? AND name IS NOT NULL ORDER BY ts DESC LIMIT 1",
                              (gid,)).fetchone()
        top_guilds.append({'id': gid, 'name': g.name if g else (last[0] if last else '이름을 모르는 서버'), 'icon': guild_icon(gid, g.icon and g.icon.key) if g else None,
                           'members': g.member_count if g else None, 'count': count, 'zooms': zoom_by_guild.get(gid, 0),
                           'bar': round(count / by_guild.most_common(1)[0][1] * 100, 1), 'gone': g is None})

    events = [dict(e) for e in db.execute(
        "SELECT ts, guild_id, name, members, kind FROM guild_events ORDER BY ts DESC LIMIT 12").fetchall()]
    for e in events:
        e['when'] = datetime.fromtimestamp(e['ts'], KST)
    joins = db.execute("SELECT COUNT(*) FROM guild_events WHERE kind = 'join' AND ts >= ?", (since,)).fetchone()[0]
    leaves = db.execute("SELECT COUNT(*) FROM guild_events WHERE kind = 'leave' AND ts >= ?", (since,)).fetchone()[0]
    stamp = datetime.now(KST)

    return render(
        request, 'admin.html',
        rail=await rail(request),
        guild_count=len(client.guilds),
        joins=joins,
        leaves=leaves,
        usage=usage,
        errors=errors,
        # 점검 중 실패는 분모에서도 뺌
        error_rate=errors / (len(rows) - down) * 100 if len(rows) > down else 0,
        down=down,
        uptime=client.uptime,
        stamp=f"{stamp:%H:%M}",
        commands=commands,
        top_guilds=top_guilds,
        events=events,
    )


def document(name: str):
    async def handler(request: web.Request):
        return render(request, name, rail=await rail(request))
    return handler


async def health(request: web.Request):
    """외부 감시용. 프로세스가 살아 있어도 디스코드 연결이 끊기면 503"""
    latency = client.latency
    ok = client.is_ready() and not client.is_closed() and latency == latency and latency < 10
    return web.json_response({'ok': ok, 'latency_ms': round(latency * 1000) if latency == latency and latency != float('inf') else None},
                             status=200 if ok else 503)


_cut_cache: dict = {}
_cut_lock = asyncio.Lock()
# bser 장애 중에는 요청마다 다시 계산하지 않고 마지막 값을 줌
CUT_RETRY_AFTER_FAIL = 60


def _cut_due() -> bool:
    now = time.time()
    return now - _cut_cache.get('at', 0) > 300 and now - _cut_cache.get('failed', 0) > CUT_RETRY_AFTER_FAIL


async def api_cut(request: web.Request):
    """labs.mongsil.dev 이터컷 예측 페이지용 JSON. 5분 동안 같은 값"""
    from commands.rating import cut_snapshot
    # 계산 중에는 지난 값이 있으면 기다리지 않고 바로 줌
    if _cut_due() and not (_cut_lock.locked() and 'data' in _cut_cache):
        async with _cut_lock:
            if _cut_due():
                data = None
                try:
                    data = await cut_snapshot(client)
                except BotError as e:
                    logger.warning(f"이터컷 JSON 계산 실패: {e}")
                except Exception:
                    logger.error("이터컷 JSON 계산 오류", exc_info=True)
                if data:
                    _cut_cache.update(at=time.time(), data=data)
                else:
                    _cut_cache['failed'] = time.time()
    data = _cut_cache.get('data')
    if not data:
        return web.json_response({'error': 'unavailable'}, status=503)
    return web.json_response(data, headers={'Cache-Control': 'public, max-age=300'})


async def invite(request: web.Request):
    """봇 추가 링크. ?guild=서버ID면 그 서버를 미리 고름"""
    gid = request.query.get('guild', '')
    raise web.HTTPFound(invite_url(int(gid) if re.fullmatch(r'[0-9]{1,20}', gid) else None))


def redirect_to(path: str, permanent: bool = True):
    async def handler(request: web.Request):
        raise (web.HTTPMovedPermanently if permanent else web.HTTPFound)(path)
    return handler


async def preview_image(request: web.Request):
    data = preview_images.get(request.match_info['key'])
    if data is None:
        raise web.HTTPNotFound()
    return web.Response(body=data, content_type='image/png',
                        headers={'Cache-Control': 'public, max-age=31536000, immutable'})


SAME_SITE = {'and': [{'href_matches': '/*'}, {'not': {'href_matches': '/log*'}}, {'not': {'href_matches': '/auth/*'}},
                     {'not': {'href_matches': '/invite*'}}, {'not': {'href_matches': '/support'}}, {'not': {'href_matches': '/discord'}}]}
# 서버 줄과 내 서버 목록은 화면이 표시되자마자 받아 두어 서버를 바꿀 때 왕복을 기다리지 않음. 나머지는 마우스를 올리면 미리 그림
SPECULATION = json.dumps({
    'prefetch': [{'where': {'and': [SAME_SITE, {'selector_matches': '.rail a, .glist a'}]}, 'eagerness': 'eager'}],
    'prerender': [{'where': SAME_SITE, 'eagerness': 'moderate'}],
})


async def speculation(request: web.Request):
    return web.Response(text=SPECULATION, content_type='application/speculationrules+json',
                        headers={'Cache-Control': 'public, max-age=86400'})


async def favicon(request: web.Request):
    if not client.user:
        raise web.HTTPNotFound()
    raise web.HTTPFound(client.user.display_avatar.with_size(64).url)


async def start_dashboard(bot: discord.Client) -> web.AppRunner:
    global client, base_url, client_secret, http
    client = bot
    base_url = os.environ['DASHBOARD_URL'].rstrip('/')
    client_secret = os.environ['DASHBOARD_CLIENT_SECRET']
    http = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10),
        headers={'User-Agent': f'DiscordBot ({base_url}, 1.0)'},
    )

    global SESSION_COOKIE, STATE_COOKIE
    if base_url.startswith('https'):
        SESSION_COOKIE, STATE_COOKIE = '__Host-mb_session', '__Host-mb_state'
    app = web.Application(middlewares=[session_middleware, ready_middleware, error_middleware])
    app.router.add_get('/', index)
    app.router.add_get('/login', login)
    app.router.add_get('/auth/callback', callback)
    app.router.add_post('/logout', logout)
    app.router.add_get('/servers', servers)
    app.router.add_get('/servers/{guild_id}', server_detail)
    app.router.add_post('/servers/{guild_id}/emoji-zoom', toggle_zoom)
    app.router.add_post('/servers/{guild_id}/settings', save_setting)
    app.router.add_get('/admin', admin)
    app.router.add_get('/favicon.ico', favicon)
    app.router.add_get('/preview/{key}.png', preview_image)
    app.router.add_get('/terms', document('terms.html'))
    app.router.add_get('/privacy', document('privacy.html'))
    app.router.add_get('/invite', invite)
    app.router.add_get('/support', redirect_to(config.support_server, permanent=False))
    app.router.add_get('/discord', redirect_to(config.support_server, permanent=False))
    app.router.add_get('/tos', redirect_to('/terms'))
    app.router.add_get('/privacy-policy', redirect_to('/privacy'))
    app.router.add_get('/speculation.json', speculation)
    app.router.add_get('/health', health)
    app.router.add_get('/api/cut', api_cut)
    app.router.add_static('/static', ROOT / 'static')
    app.on_cleanup.append(lambda _: http.close())
    refresh = asyncio.create_task(previews.refresh_loop(client))

    async def stop_refresh(_):
        refresh.cancel()
    app.on_cleanup.append(stop_refresh)

    runner = web.AppRunner(app, access_log=None, shutdown_timeout=5)
    await runner.setup()
    port = int(os.getenv('DASHBOARD_PORT', '8095'))
    await web.TCPSite(runner, os.getenv('DASHBOARD_HOST', '0.0.0.0'), port).start()
    logger.info(f"대시보드 시작: {base_url} (포트 {port})")
    return runner
