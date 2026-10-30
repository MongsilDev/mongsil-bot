import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import CODE, OTHER, FakeChannel, FakeClient, add_board, board, db, edited_text, reset, t, text_of

import discord


def link(cid, group='A'):
    return {'group': group, 'channelId': str(cid), 'guild': '서버', 'channel': f'채널{cid}'}


class LinkSyncTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset()
        self.ch1, self.ch2 = FakeChannel(101), FakeChannel(102)
        self.cog = t.Tournament(FakeClient(self.ch1, self.ch2))
        self.patches = [
            mock.patch.object(t, 'fetch_board', mock.AsyncMock(side_effect=lambda c, g=None, *a, **k: board(c, g))),
            mock.patch.object(t, 'fetch_image', mock.AsyncMock(return_value=b'png')),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def changed(self, links):
        return mock.patch.object(t, 'fetch_links', mock.AsyncMock(return_value=links))

    def test_diff(self):
        added, removed = t.diff_links({1: (CODE, 'A'), 2: (CODE, 'B')}, {1: (CODE, 'A'), 3: (CODE, 'B')})
        self.assertEqual(added, {3: (CODE, 'B')})
        self.assertEqual(removed, {2: (CODE, 'B')})

    async def test_new_link_posts_notice(self):
        with self.changed([link(101)]):
            await self.cog.sync_code(CODE)
        self.assertEqual(len(self.ch1.sent), 1)
        self.assertIn(101, t.linked)
        row = db.execute("SELECT * FROM live_boards").fetchone()
        self.assertEqual((row['channel_id'], row['started_by']), (101, 0))

    async def test_removed_link_stops_board(self):
        t.copy_set(101, 1, CODE, 'A')
        mid = add_board(self.ch1)
        with self.changed([]):
            await self.cog.sync_code(CODE)
        self.assertIsNone(t.live_get(mid))
        self.assertNotIn(101, t.linked)
        self.assertIn("더 이상 순위를 올리지 않습니다.", edited_text(self.ch1, mid))

    async def test_full_sync_retries_failed_notice(self):
        down = mock.AsyncMock(side_effect=t.ServiceDownError('x', 'down'))
        with self.changed([link(101)]), mock.patch.object(t, 'fetch_board', down):
            await self.cog.sync_code(CODE)
        self.assertEqual((self.ch1.sent, t.copy_rows()), ([], {101: (CODE, 'A')}))
        with mock.patch.object(t, 'fetch_all_links', mock.AsyncMock(return_value=[{'code': CODE, **link(101)}])):
            await self.cog.sync_all()
        self.assertEqual(len(self.ch1.sent), 1)
        self.assertEqual(t._retry, {})

    async def test_full_sync_drops_retry_after_unlink(self):
        down = mock.AsyncMock(side_effect=t.ServiceDownError('x', 'down'))
        with self.changed([link(101)]), mock.patch.object(t, 'fetch_board', down):
            await self.cog.sync_code(CODE)
        with mock.patch.object(t, 'fetch_all_links', mock.AsyncMock(return_value=[])), self.changed([]):
            await self.cog.sync_all()
        self.assertEqual((self.ch1.sent, t._retry, t.copy_rows()), ([], {}, {}))

    async def test_same_link_does_nothing(self):
        t.copy_set(101, 1, CODE, 'A')
        mid = add_board(self.ch1)
        with self.changed([link(101)]):
            await self.cog.sync_code(CODE)
        self.assertEqual((self.ch1.sent, self.ch1.edits), ([], []))
        self.assertIsNotNone(t.live_get(mid))

    async def test_moved_link(self):
        t.copy_set(101, 1, CODE, 'A')
        mid = add_board(self.ch1)
        with self.changed([link(102)]):
            await self.cog.sync_code(CODE)
        self.assertIn("더 이상 순위를 올리지", edited_text(self.ch1, mid))
        self.assertEqual(len(self.ch2.sent), 1)

    async def test_replaced_by_other_tournament(self):
        t.copy_set(101, 1, OTHER, 'B')
        mid = add_board(self.ch1, OTHER, 'B')
        with self.changed([link(101)]):
            await self.cog.sync_code(CODE)
        self.assertIn("더 이상 순위를 올리지", edited_text(self.ch1, mid))
        self.assertEqual(t.copy_rows(), {101: (CODE, 'A')})
        self.assertEqual(len(self.ch1.sent), 1)

    async def test_missing_permission_keeps_link_only(self):
        self.ch1.perms = discord.Permissions(view_channel=True, send_messages=True)
        with self.changed([link(101)]):
            await self.cog.sync_code(CODE)
        self.assertEqual(self.ch1.sent, [])
        self.assertIn(101, t.linked)

    async def test_gone_tournament(self):
        t.copy_set(101, 1, CODE, 'A')
        mid = add_board(self.ch1)
        with mock.patch.object(t, 'fetch_links', mock.AsyncMock(side_effect=t.NotFoundError('x', t.GONE))):
            await self.cog.sync_code(CODE)
        self.assertIn("삭제된 대회입니다.", edited_text(self.ch1, mid))
        self.assertNotIn(101, t.linked)

    async def test_full_sync_never_posts(self):
        t.copy_set(102, 1, CODE, 'B')
        mid = add_board(self.ch2, CODE, 'B')
        all_links = [{'channelId': '101', 'code': CODE, 'group': 'A'}]
        with mock.patch.object(t, 'fetch_all_links', mock.AsyncMock(return_value=all_links)), \
                mock.patch.object(t, 'fetch_board', mock.AsyncMock(return_value=t.UNCHANGED)):
            await self.cog.sync_all()
        self.assertEqual((self.ch1.sent, self.ch2.sent), ([], []))
        self.assertEqual(t.copy_rows(), {101: (CODE, 'A')})
        self.assertEqual(t._retry, {101: (CODE, 'A')})
        self.assertIsNone(t.live_get(mid))
        self.assertIn("더 이상 순위를 올리지", edited_text(self.ch2, mid))

    async def test_changed_after_full_sync_posts_notice(self):
        all_links = [{'code': CODE, **link(101)}]
        with mock.patch.object(t, 'fetch_all_links', mock.AsyncMock(return_value=all_links)):
            await self.cog.sync_all()
        self.assertEqual(self.ch1.sent, [])
        with self.changed([link(101)]):
            await self.cog.sync_code(CODE)
            await self.cog.sync_code(CODE)
        self.assertEqual((len(self.ch1.sent), t._retry), (1, {}))

    async def test_full_sync_posts_missed_notice_next_time(self):
        all_links = [{'code': CODE, **link(101)}]
        with mock.patch.object(t, 'fetch_all_links', mock.AsyncMock(return_value=all_links)):
            await self.cog.sync_all()
            self.assertEqual(self.ch1.sent, [])
            await self.cog.sync_all()
        self.assertEqual((len(self.ch1.sent), t._retry), (1, {}))

    async def test_full_sync_keeps_ended_link(self):
        t.copy_set(101, 1, CODE, 'A')
        mid = add_board(self.ch1)
        with mock.patch.object(t, 'fetch_all_links', mock.AsyncMock(return_value=[])), self.changed([link(101)]):
            await self.cog.sync_all()
        self.assertEqual(t.copy_rows(), {101: (CODE, 'A')})
        self.assertIn(101, t.linked)
        self.assertIsNotNone(t.live_get(mid))

    async def test_full_sync_drops_gone_link(self):
        t.copy_set(101, 1, CODE, 'A')
        mid = add_board(self.ch1)
        gone = mock.AsyncMock(side_effect=t.NotFoundError('x', t.GONE))
        with mock.patch.object(t, 'fetch_all_links', mock.AsyncMock(return_value=[])), \
                mock.patch.object(t, 'fetch_links', gone), mock.patch.object(t, 'fetch_board', gone):
            await self.cog.sync_all()
        self.assertEqual(t.copy_rows(), {})
        self.assertIn("삭제된 대회입니다.", edited_text(self.ch1, mid))



class OperatorOnlyTest(unittest.TestCase):
    def test_only_owner_and_operator(self):
        for role in ('owner', 'operator'):
            t.check_operator(board(CODE, 'B') | {'role': role})
        with self.assertRaises(t.ValidationError) as e:
            t.check_operator(board(CODE, 'B') | {'role': None})
        self.assertEqual(e.exception.user_message, t.NOT_OPERATOR)

    def test_example_is_open(self):
        t.check_operator(board(CODE, 'A') | {'code': 'EXAMPLE', 'role': None})



class ParseTargetTest(unittest.TestCase):
    def test_key_address(self):
        key = '0123456789abcdef01234567'
        self.assertEqual(t.parse_target(f'https://labs.example/er/t/{CODE}/{key}'), (CODE, key))
        self.assertEqual(t.parse_target(f'https://labs.example/er/t/{CODE}/{key}/overlay'), (CODE, key))

    def test_code_and_letter_address(self):
        self.assertEqual(t.parse_target(CODE), (CODE, None))
        self.assertEqual(t.parse_target(f'https://labs.example/er/t/{CODE}/b'), (CODE, 'B'))


class OperatorLinkTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset()

    async def link(self, *args):
        ch = FakeChannel(101)
        cog = t.Tournament(FakeClient(ch))
        b = board(group='B', groups=2)
        response = SimpleNamespace(is_done=lambda: False, send_message=mock.AsyncMock())
        inter = SimpleNamespace(channel=ch, guild=ch.guild, user=SimpleNamespace(id=5), extras={},
                                app_permissions=discord.Permissions.all(), response=response,
                                followup=SimpleNamespace(send=mock.AsyncMock()), created_at=discord.utils.utcnow(),
                                original_response=mock.AsyncMock(return_value=SimpleNamespace(id=900)))
        put = mock.AsyncMock(return_value=[])
        with mock.patch.object(t, 'fetch_board', mock.AsyncMock(return_value=b)) as fetch, \
                mock.patch.object(t, 'fetch_image', mock.AsyncMock(return_value=b'png')), \
                mock.patch.object(t, 'put_links', put):
            await t.Tournament.tournament_command.callback(cog, inter, *args)
        return fetch.await_args.args[1], put.await_args.kwargs.get('group') or put.await_args.args[-1]

    async def test_code_and_group(self):
        self.assertEqual(await self.link(CODE, SimpleNamespace(value='B')), ('B', 'B'))

    async def test_key_address(self):
        key = '0123456789abcdef01234567'
        self.assertEqual(await self.link(f'https://labs.example/er/t/{CODE}/{key}'), (key, 'B'))


class NoCodeTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_code_guides_privately(self):
        cog = SimpleNamespace()
        send = mock.AsyncMock()
        inter = SimpleNamespace(response=SimpleNamespace(send_message=send))
        with mock.patch.object(t, 'fetch_board', mock.AsyncMock()) as fetch:
            await t.Tournament.tournament_command.callback(cog, inter, None, SimpleNamespace(value='B'))
        fetch.assert_not_awaited()
        self.assertTrue(send.await_args.kwargs['ephemeral'])
        view = send.await_args.kwargs['view']
        self.assertIn(t.NO_CODE, text_of(view))
        self.assertTrue(any(isinstance(i, discord.ui.Button) and i.url.endswith('/er/score') for i in view.walk_children()))


if __name__ == '__main__':
    unittest.main()
