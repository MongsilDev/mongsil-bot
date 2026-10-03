"""
웹 대시보드. 봇 프로세스 안에서 aiohttp로 돌아 봇 상태와 설정 캐시를 그대로 공유
"""
import asyncio
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
from aiohttp import web

from utils.config import config
from utils.emoji_zoom import load_disabled_servers, save_disabled_servers
from utils.logging_config import get_logger
from utils.usage_db import db
from web import previews

logger = get_logger('대시보드')

API = 'https://discord.com/api/v10'
ROOT = Path(__file__).parent
KST = timezone(timedelta(hours=9))
SERVICE_START = datetime(2023, 6, 15, tzinfo=KST)

ADMINISTRATOR = 1 << 3
MANAGE_GUILD = 1 << 5

SESSION_COOKIE = 'mb_session'
STATE_COOKIE = 'mb_state'
SESSION_TTL = 7 * 86400
GUILDS_TTL = 60
GUILD_INFO_TTL = 300

VERIFICATION = {'none': '없음', 'low': '낮음', 'medium': '중간', 'high': '높음', 'highest': '매우 높음'}
LOCALES = {'ko': '한국어', 'en-US': '영어', 'en-GB': '영어', 'ja': '일본어', 'zh-CN': '중국어 간체', 'zh-TW': '중국어 번체',
           'ru': '러시아어', 'de': '독일어', 'fr': '프랑스어', 'es-ES': '스페인어', 'pt-BR': '포르투갈어', 'vi': '베트남어', 'th': '태국어'}
FEATURES = {'COMMUNITY': '커뮤니티', 'PARTNERED': '파트너', 'VERIFIED': '인증됨', 'DISCOVERABLE': '서버 찾기'}

# 봇에서 자주 쓰는 순서, 목록에 없는 명령은 맨 뒤
COMMAND_ORDER = ['랭크', '랭킹', '이터컷', '플탐', '시즌', '동접', '계정', '강아지', '고양이', '설정', '정보']

client: discord.Client = None
base_url = ''
client_secret = ''
http: aiohttp.ClientSession = None
guild_cache: dict[str, tuple[float, list]] = {}
guild_info_cache: dict[int, tuple[float, dict]] = {}

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
env.filters['shortdate'] = lambda d: f"{d.month}월 {d.day}일 {d:%H:%M}"
env.globals['session_avatar'] = lambda s: avatar_url(s['user_id'], s['avatar'])
env.globals['support_server'] = config.support_server
env.globals['bot_avatar'] = lambda size: client.user.display_avatar.with_size(size).url if client.user else '/favicon.ico'
env.globals['static_version'] = str(int(max(f.stat().st_mtime for f in (ROOT / 'static').iterdir())))


def render(request: web.Request, name: str, status: int = 200, **context) -> web.Response:
    session = request.get('session')
    html = env.get_template(name).render(
        session=session,
        is_owner=bool(session) and session['user_id'] in owner_ids(),
        path=request.path,
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


def usage_summary(cmd_rows, zoom_rows, now: int) -> dict:
    """30일 사용량 타일과 차트. 행은 60일치를 받아 앞 30일과 비교"""
    since, prev = now - 30 * 86400, now - 60 * 86400
    cur = [r for r in cmd_rows if r['ts'] >= since]
    old = [r for r in cmd_rows if prev <= r['ts'] < since]
    zcur = [r for r in zoom_rows if r['ts'] >= since]
    zold = [r for r in zoom_rows if prev <= r['ts'] < since]
    cmd_first, zoom_first = first_ts('command_log'), first_ts('zoom_log')
    cmd_full = bool(cmd_first) and cmd_first <= prev
    zoom_full = bool(zoom_first) and zoom_first <= prev
    today, yesterday = day_start(), day_start(1)
    users, old_users = {r['user_id'] for r in cur}, {r['user_id'] for r in old}
    cmd_since = datetime.fromtimestamp(cmd_first or now, KST) if not cmd_full else None
    zoom_since = datetime.fromtimestamp(zoom_first or now, KST) if not zoom_full else None
    return {
        'commands': {'value': len(cur), 'delta': delta(len(cur), len(old), cmd_full), 'since': cmd_since},
        'users': {'value': len(users), 'delta': delta(len(users), len(old_users), cmd_full), 'since': cmd_since},
        'zooms': {'value': len(zcur), 'delta': delta(len(zcur), len(zold), zoom_full), 'since': zoom_since},
        'today': {'value': sum(1 for r in cur if r['ts'] >= today),
                  'yesterday': sum(1 for r in cur if yesterday <= r['ts'] < today)},
        'cmd_chart': daily_series(cur),
        'zoom_chart': daily_series(zcur),
        'cmd_since': datetime.fromtimestamp(cmd_first, KST) if cmd_first and cmd_first > since else None,
        'zoom_since': datetime.fromtimestamp(zoom_first or now, KST) if not zoom_first or zoom_first > since else None,
        'rows': cur,
        'zoom_rows': zcur,
    }


# ---------- 세션 ----------

@web.middleware
async def session_middleware(request: web.Request, handler):
    sid = request.cookies.get(SESSION_COOKIE)
    request['session'] = None
    if sid:
        row = db.execute("SELECT * FROM sessions WHERE id = ? AND expires > ?", (sid, int(time.time()))).fetchone()
        if row:
            request['session'] = dict(row)
    response = await handler(request)
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('Referrer-Policy', 'same-origin')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    if not request.path.startswith('/static/'):
        response.headers.setdefault('Cache-Control', 'private, no-store')
    response.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; img-src 'self' https: data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; form-action 'self'",
    )
    return response


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPNotFound:
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
        raise web.HTTPFound('/login?' + urlencode({'next': request.path}))
    return request['session']


def check_csrf(request: web.Request, token: str | None):
    session = request['session']
    if not session or not token or not secrets.compare_digest(token, session['csrf']):
        raise web.HTTPForbidden(text='CSRF')


async def user_guilds(request: web.Request) -> list[dict]:
    """로그인 유저의 서버 목록. Discord 호출은 세션당 60초 캐시"""
    session = request['session']
    cached = guild_cache.get(session['id'])
    if cached and time.time() - cached[0] < GUILDS_TTL:
        return cached[1]

    async with http.get(f"{API}/users/@me/guilds", headers={'Authorization': f"Bearer {session['token']}"}) as r:
        if r.status == 401:
            db.execute("DELETE FROM sessions WHERE id = ?", (session['id'],))
            raise web.HTTPFound('/login?' + urlencode({'next': request.path}))
        if r.status == 429 and cached:
            return cached[1]
        r.raise_for_status()
        guilds = await r.json()

    guild_cache[session['id']] = (time.time(), guilds)
    return guilds


def can_manage(g: dict) -> bool:
    perms = int(g.get('permissions', 0))
    return g.get('owner') or bool(perms & (ADMINISTRATOR | MANAGE_GUILD))


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
    if not state or not secrets.compare_digest(state, request.query.get('state', '')) or 'code' not in request.query:
        return error_page(request, 400, "로그인하지 못했습니다", "처음 화면에서 다시 로그인해주세요.")

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

    sid = secrets.token_urlsafe(32)
    expires = int(time.time()) + min(SESSION_TTL, int(token.get('expires_in', SESSION_TTL)))
    db.execute("DELETE FROM sessions WHERE expires < ?", (int(time.time()),))
    db.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, int(user['id']), user.get('global_name') or user['username'], user.get('avatar'),
         token['access_token'], secrets.token_urlsafe(24), expires),
    )
    logger.info(f"대시보드 로그인: {user['username']} ({user['id']})")

    response = web.HTTPFound(nxt or '/servers')
    response.set_cookie(SESSION_COOKIE, sid, max_age=expires - int(time.time()), **cookie_opts())
    response.del_cookie(STATE_COOKIE, path='/')
    raise response


async def logout(request: web.Request):
    form = await request.post()
    check_csrf(request, form.get('csrf'))
    db.execute("DELETE FROM sessions WHERE id = ?", (request['session']['id'],))
    guild_cache.pop(request['session']['id'], None)
    response = web.HTTPFound('/')
    response.del_cookie(SESSION_COOKIE, path='/')
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
        stamp = f"{'오전' if t.hour < 12 else '오후'} {t.hour % 12 or 12}:{t.minute:02d}"

    return render(
        request, 'index.html',
        avatar=client.user.display_avatar.with_size(128).url if client.user else None,
        guild_count=len(client.guilds),
        user_count=sum(g.member_count or 0 for g in client.guilds),
        days=(datetime.now(KST) - SERVICE_START).days,
        commands=commands,
        selected='랭킹',
        previews=previews.previews,
        live=live,
        stamp=stamp,
        invite=invite_url(),
    )


async def servers(request: web.Request):
    require_login(request)
    guilds = [g for g in await user_guilds(request) if can_manage(g)]
    since = int(time.time()) - 30 * 86400
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
            item['invite'] = invite_url(gid)
            others.append(item)
    # 많이 쓰는 서버가 위로, 같으면 이름순
    joined.sort(key=lambda g: -(g['usage'] + g['zooms']))
    return render(request, 'servers.html', joined=joined, others=others)


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


async def guild_counts(guild: discord.Guild) -> dict:
    """온라인 수와 소유자 이름. 게이트웨이 캐시에 없어 REST로 받고 5분 캐시"""
    cached = guild_info_cache.get(guild.id)
    if cached and time.time() - cached[0] < GUILD_INFO_TTL:
        return cached[1]
    extra = {}
    try:
        full = await client.fetch_guild(guild.id, with_counts=True)
        extra['online'] = full.approximate_presence_count
    except discord.HTTPException as e:
        logger.warning(f"서버 인원 조회 실패: {guild.id} {e}")
    try:
        owner = guild.get_member(guild.owner_id) or await guild.fetch_member(guild.owner_id)
        extra['owner'] = owner.display_name
    except discord.HTTPException as e:
        logger.warning(f"서버 소유자 조회 실패: {guild.id} {e}")
    guild_info_cache[guild.id] = (time.time(), extra)
    return extra


async def server_detail(request: web.Request):
    guild = await managed_guild(request)
    extra = await guild_counts(guild)
    perms = guild.me.guild_permissions
    info = {
        'banner': guild.banner.with_size(1024).url if guild.banner else None,
        'description': guild.description,
        'features': [FEATURES[f] for f in FEATURES if f in guild.features],
        'online': extra.get('online'),
        'owner': extra.get('owner'),
        'boost_tier': guild.premium_tier,
        'boosts': guild.premium_subscription_count or 0,
        'text': len(guild.text_channels) + len(guild.forums),
        'voice': len(guild.voice_channels) + len(guild.stage_channels),
        'roles': max(len(guild.roles) - 1, 0),
        'emojis': len(guild.emojis),
        'emoji_limit': guild.emoji_limit,
        'stickers': len(guild.stickers),
        'sticker_limit': guild.sticker_limit,
        'created': guild.created_at.astimezone(KST),
        'verification': VERIFICATION.get(guild.verification_level.name, guild.verification_level.name),
        'locale': LOCALES.get(str(guild.preferred_locale), str(guild.preferred_locale)),
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
    usage = usage_summary(cmd_rows, zoom_rows, now)

    return render(
        request, 'server.html',
        guild={'id': guild.id, 'name': guild.name, 'icon': guild_icon(guild.id, guild.icon and guild.icon.key),
               'members': guild.member_count,
               'joined_at': guild.me.joined_at.astimezone(KST) if guild.me and guild.me.joined_at else None},
        info=info,
        permissions=permissions,
        is_admin=perms.administrator,
        zoom_enabled=guild.id not in load_disabled_servers(),
        can_webhook=perms.manage_webhooks,
        usage=usage,
        by_command=tally(r['command'] for r in usage['rows']),
    )


async def toggle_zoom(request: web.Request):
    guild = await managed_guild(request)
    check_csrf(request, request.headers.get('X-CSRF-Token'))
    body = await request.json()
    enable = bool(body.get('enabled'))

    disabled = set(load_disabled_servers())
    if enable:
        if not guild.me.guild_permissions.manage_webhooks:
            return web.json_response(
                {'enabled': False,
                 'error': "봇에 웹후크 관리 권한이 없어 켤 수 없습니다. 몽실봇 역할에 웹후크 관리 권한을 준 뒤 다시 시도해주세요."},
                status=409,
            )
        disabled.discard(guild.id)
    else:
        disabled.add(guild.id)

    if not save_disabled_servers(disabled):
        return web.json_response({'enabled': not enable, 'error': "저장하지 못했습니다. 잠시 후 다시 시도해주세요."}, status=500)
    logger.info(f"대시보드 설정 변경: {guild.name} ({guild.id}) 이모지 확대 {'켬' if enable else '끔'}")
    return web.json_response({'enabled': enable})


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
        item.update(errors=failed, error_rate=failed / len(mine) * 100,
                    median=percentile(source, 0.5), p95=percentile(source, 0.95),
                    basis='표시' if source is shown else '처리')
        commands.append(item)

    by_guild = Counter(r['guild_id'] for r in rows if r['guild_id'])
    zoom_by_guild = Counter(r['guild_id'] for r in usage['zoom_rows'])
    top_guilds = []
    for gid, count in by_guild.most_common(10):
        g = client.get_guild(gid)
        top_guilds.append({'id': gid, 'name': g.name if g else str(gid), 'icon': guild_icon(gid, g.icon and g.icon.key) if g else None,
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
        guild_count=len(client.guilds),
        joins=joins,
        leaves=leaves,
        usage=usage,
        errors=errors,
        error_rate=errors / len(rows) * 100 if rows else 0,
        down=down,
        uptime=client.uptime,
        stamp=f"{stamp:%H:%M}",
        commands=commands,
        top_guilds=top_guilds,
        events=events,
    )


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

    app = web.Application(middlewares=[session_middleware, error_middleware])
    app.router.add_get('/', index)
    app.router.add_get('/login', login)
    app.router.add_get('/auth/callback', callback)
    app.router.add_post('/logout', logout)
    app.router.add_get('/servers', servers)
    app.router.add_get('/servers/{guild_id}', server_detail)
    app.router.add_post('/servers/{guild_id}/emoji-zoom', toggle_zoom)
    app.router.add_get('/admin', admin)
    app.router.add_get('/favicon.ico', favicon)
    app.router.add_static('/static', ROOT / 'static')
    app.on_cleanup.append(lambda _: http.close())
    refresh = asyncio.create_task(previews.refresh_loop(client))

    async def stop_refresh(_):
        refresh.cancel()
    app.on_cleanup.append(stop_refresh)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = int(os.getenv('DASHBOARD_PORT', '8095'))
    await web.TCPSite(runner, os.getenv('DASHBOARD_HOST', '0.0.0.0'), port).start()
    logger.info(f"대시보드 시작: {base_url} (포트 {port})")
    return runner
