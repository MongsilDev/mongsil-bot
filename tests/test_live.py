import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import CODE, FakeChannel, FakeClient, FakeGuild, add_board, board, db, edited_text, reset, t, text_of

import discord


class ChannelListTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset()

    def test_permission_check(self):
        full = discord.Permissions(view_channel=True, send_messages=True, read_message_history=True,
                                   attach_files=True)
        user = discord.Permissions(view_channel=True, send_messages=True)
        self.assertTrue(t.channel_allowed(user, full))
        self.assertFalse(t.channel_allowed(discord.Permissions(view_channel=True), full))
        for missing in ('view_channel', 'send_messages', 'read_message_history', 'attach_files'):
            bot = discord.Permissions(full.value)
            setattr(bot, missing, False)
            self.assertFalse(t.channel_allowed(user, bot), missing)

    async def test_failed_guild_left_out_and_cached(self):
        ok, gone, other = FakeGuild(1, '나 서버'), FakeGuild(2, '가 서버'), FakeGuild(3, '다 서버')
        ok.fetch_member = mock.AsyncMock(return_value=object())
        gone.fetch_member = mock.AsyncMock(side_effect=discord.NotFound(SimpleNamespace(status=404, reason=''), ''))
        other.fetch_member = mock.AsyncMock(return_value=object())
        client = FakeClient()
        client.guilds = {1: ok, 2: gone, 3: other}
        db.execute("INSERT INTO command_log (ts, guild_id, user_id, command, status) VALUES (0, 1, 7, '대회', 'ok')")
        db.execute("INSERT INTO command_log (ts, guild_id, user_id, command, status) VALUES (0, 2, 7, '대회', 'ok')")
        db.execute("INSERT INTO command_log (ts, guild_id, user_id, command, status) VALUES (0, 3, 7, '대회', 'ok')")
        db.execute("INSERT INTO command_log (ts, guild_id, user_id, command, status) VALUES (0, 9, 7, '대회', 'ok')")
        chans = [{'id': '5', 'name': '결과'}]
        with mock.patch.object(t, 'guild_channels', return_value=chans):
            result = await t.channel_list(client, 7)
            again = await t.channel_list(client, 7)
        self.assertEqual([g['name'] for g in result['guilds']], ['나 서버', '다 서버'])
        self.assertIs(again, result)
        ok.fetch_member.assert_awaited_once()


class RefreshTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset()
        self.ch = FakeChannel(101)
        self.cog = t.Tournament(FakeClient(self.ch))

    async def test_one_refresh_per_board(self):
        mid = add_board(self.ch)
        gate, calls = asyncio.Event(), []

        async def tick(row):
            calls.append(row['message_id'])
            await gate.wait()

        with mock.patch.object(self.cog, 'tick', tick):
            first = asyncio.create_task(self.cog.refresh(mid))
            await asyncio.sleep(0)
            await asyncio.gather(self.cog.refresh(mid), self.cog.refresh(mid))
            self.assertEqual(calls, [mid])
            gate.set()
            await first
        self.assertEqual(calls, [mid, mid])

    async def test_notice_edited_only_when_changed(self):
        t.copy_set(101, 1, CODE, 'A')
        mid = add_board(self.ch, notice=None)
        with mock.patch.object(t, 'fetch_board', mock.AsyncMock(return_value=t.UNCHANGED)):
            await self.cog.refresh(mid)
            await self.cog.refresh(mid)
        # 배포 전 보드는 첫 tick에서 한 번만 새 형식으로 고침
        self.assertEqual(len(self.ch.edits), 1)
        b = board(version=2)
        with mock.patch.object(t, 'fetch_board', mock.AsyncMock(return_value=b)):
            await self.cog.refresh(mid)
        self.assertEqual(len(self.ch.edits), 1)
        with mock.patch.object(t, 'fetch_board', mock.AsyncMock(return_value={**b, 'version': 3, 'plannedRounds': 5})):
            await self.cog.refresh(mid)
        self.assertEqual(len(self.ch.edits), 2)

    async def test_stop_during_upload_keeps_stopped_notice(self):
        t.copy_set(101, 1, CODE, 'A')
        mid = add_board(self.ch)

        async def upload(channel, b, png, image_id):
            await self.cog.stop(t.live_get(mid), 'unlinked')
            return 900

        with mock.patch.object(t, 'fetch_board', mock.AsyncMock(return_value=board(played=1, version=2))), \
                mock.patch.object(t, 'fetch_image', mock.AsyncMock(return_value=b'png')), \
                mock.patch.object(t, 'post_round', upload):
            await self.cog.refresh(mid)
        self.assertIn("연결을 해제해", edited_text(self.ch, mid))


class CommandTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset()
        self.ch = FakeChannel(101)
        self.cog = t.Tournament(FakeClient(self.ch))

    async def test_blocked_operator_gets_card(self):
        sent, private = [], []

        async def send_message(**kwargs):
            sent.append(kwargs)
            response.is_done = lambda: True
            return SimpleNamespace(resource=None)

        async def followup_send(**kwargs):
            private.append(kwargs)

        response = SimpleNamespace(is_done=lambda: False, send_message=send_message)
        inter = SimpleNamespace(channel=self.ch, guild=self.ch.guild, user=SimpleNamespace(id=5),
                                app_permissions=discord.Permissions.none(), response=response,
                                followup=SimpleNamespace(send=followup_send), extras={},
                                created_at=discord.utils.utcnow(),
                                original_response=mock.AsyncMock(return_value=SimpleNamespace(id=900)))
        put = mock.AsyncMock()
        with mock.patch.object(t, 'fetch_board', mock.AsyncMock(return_value=board())), \
                mock.patch.object(t, 'fetch_image', mock.AsyncMock(return_value=b'png')), \
                mock.patch.object(t, 'put_links', put):
            await self.cog.tournament_command.callback(self.cog, inter, CODE)
        self.assertEqual(len(sent), 1)
        self.assertTrue(any(isinstance(i, discord.ui.MediaGallery) for i in sent[0]['view'].walk_children()))
        self.assertTrue(private[0]['ephemeral'])
        self.assertIn("권한이 없어", text_of(private[0]['view']))
        put.assert_not_awaited()
        self.assertEqual(t.copy_rows(), {})
        self.assertEqual(db.execute("SELECT COUNT(*) FROM live_boards").fetchone()[0], 0)


def interaction(channel, user_id=5):
    return SimpleNamespace(channel=channel, guild=channel.guild, user=SimpleNamespace(id=user_id),
                           app_permissions=discord.Permissions.all())


class RaceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset()
        self.ch = FakeChannel(101)
        self.cog = t.Tournament(FakeClient(self.ch))
        self.link = [{'group': 'A', 'channelId': '101', 'guild': '서버', 'channel': '채널101'}]

    async def test_changed_during_command_posts_once(self):
        b = board()
        changed = []

        async def put_links(code, actor, links, group=None):
            # Labs가 커밋하고 알림을 보낸 뒤 응답이 돌아옴
            changed.append(asyncio.create_task(self.cog.on_changed(code)))
            await asyncio.sleep(0.01)
            return self.link

        with mock.patch.object(t, 'put_links', put_links), \
                mock.patch.object(t, 'fetch_links', mock.AsyncMock(return_value=self.link)), \
                mock.patch.object(t, 'fetch_board', mock.AsyncMock(return_value=b)), \
                mock.patch.object(t, 'fetch_image', mock.AsyncMock(return_value=b'png')):
            live = {}
            await self.cog.link_here(interaction(self.ch), b, live)
            # /대회는 안내 메시지를 인터랙션 응답으로 올림
            await self.cog.start(self.ch, 1, b, None, 5, 999)
            t._starting.discard(live['key'])
            await asyncio.gather(*changed)
        self.assertEqual(self.ch.sent, [])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM live_boards").fetchone()[0], 1)

    async def test_stale_full_sync_keeps_new_link(self):
        b = board()
        fetched = asyncio.Event()
        release = asyncio.Event()

        async def stale_links():
            fetched.set()
            await release.wait()
            return []

        async def put_links(code, actor, links, group=None):
            return self.link

        with mock.patch.object(t, 'fetch_all_links', stale_links), mock.patch.object(t, 'put_links', put_links):
            sync = asyncio.create_task(self.cog.sync_all())
            await fetched.wait()
            command = asyncio.create_task(self.cog.link_here(interaction(self.ch), b, {}))
            await asyncio.sleep(0.01)
            release.set()
            await asyncio.gather(sync, command)
        self.assertEqual(t.copy_rows(), {101: (CODE, 'A')})
        self.assertIn(101, t.linked)

    async def test_already_linked(self):
        b = board()
        add_board(self.ch)
        with self.assertRaises(t.AlreadyLinked) as caught:
            await self.cog.link_here(interaction(self.ch), b, {})
        self.assertEqual(caught.exception.text, "이 채널은 이미 테스트 대회에 연결돼 있습니다.")


if __name__ == '__main__':
    unittest.main()
