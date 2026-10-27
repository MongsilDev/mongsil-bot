"""
테스트 공용. 디스코드와 Labs에 접속하지 않고 메모리 DB를 씀. 다른 모듈보다 먼저 import
"""
import itertools
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ['MONGSIL_DB'] = ':memory:'
os.environ.setdefault('BOT_TOKEN', 'test')
os.environ.setdefault('API_KEY', 'test')

import discord  # noqa: E402

from commands import tournament as t  # noqa: E402
from utils.usage_db import db  # noqa: E402

CODE = 'ABCDEF'
OTHER = 'GHJKMN'
_ids = itertools.count(9000)


def reset() -> None:
    for table in ('live_boards', 'result_links', 'command_log'):
        db.execute(f"DELETE FROM {table}")
    t.linked.clear()
    t._starting.clear()
    t._retry.clear()
    t._channels.clear()
    t._kept.clear()
    t._pending.clear()
    t._relaying.clear()
    t._last_refresh.clear()
    # 테스트마다 이벤트 루프가 달라 잠금도 새로 만듦
    t.links_lock = t.asyncio.Lock()


def board(code=CODE, group='A', groups=1, mode='points', played=0, planned=4, ended=False, version=1, next=None):
    return {'code': code, 'group': group, 'groupCount': groups, 'groups': list('ABCDEFGH'[:groups]),
            'name': '테스트 대회', 'mode': mode, 'played': played, 'plannedRounds': planned, 'ended': ended,
            'version': version, 'url': f'https://labs.example/er/t/{code}', 'next': next, 'role': 'owner'}


class FakeMessage:
    def __init__(self, channel, mid):
        self.channel, self.id = channel, mid

    async def edit(self, **kwargs):
        self.channel.edits.append((self.id, kwargs))

    async def delete(self):
        self.channel.deleted.append(self.id)


class FakeGuild:
    def __init__(self, gid=1, name='서버'):
        self.id, self.name = gid, name
        self.me = SimpleNamespace(id=1)


class FakeChannel:
    def __init__(self, cid, guild=None, perms=None):
        self.id, self.guild = cid, guild or FakeGuild()
        self.name = f'채널{cid}'
        self.perms = perms or discord.Permissions.all()
        self.sent, self.edits, self.deleted = [], [], []

    async def send(self, **kwargs):
        mid = next(_ids)
        self.sent.append((mid, kwargs))
        return SimpleNamespace(id=mid)

    def get_partial_message(self, mid):
        return FakeMessage(self, mid)

    def permissions_for(self, who):
        return self.perms


class FakeClient:
    def __init__(self, *channels):
        self.channels = {c.id: c for c in channels}
        self.guilds = {}

    def get_channel(self, cid):
        return self.channels.get(cid)

    def get_partial_messageable(self, cid):
        if cid not in self.channels:
            self.channels[cid] = FakeChannel(cid)
        return self.channels[cid]

    def get_guild(self, gid):
        return self.guilds.get(gid)


def add_board(channel, code=CODE, group='A', started_by=5, notice='') -> int:
    """채널에 돌고 있는 보드를 하나 만듦"""
    mid = next(_ids)
    t.live_put(mid, channel.id, channel.guild.id, board(code, group), started_by, notice)
    return mid


def text_of(view) -> str:
    return '\n'.join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))


def edited_text(channel, mid) -> str:
    return text_of([kw['view'] for m, kw in channel.edits if m == mid][-1])
