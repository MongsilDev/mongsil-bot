import unittest
from unittest import mock

from helpers import CODE, OTHER, FakeChannel, FakeClient, add_board, board, db, edited_text, reset, t

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
        self.assertIn("연결을 해제해 자동 갱신을 멈췄습니다.", edited_text(self.ch1, mid))

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
        self.assertIn("다른 채널로 연결을 옮겨", edited_text(self.ch1, mid))
        self.assertEqual(len(self.ch2.sent), 1)

    async def test_replaced_by_other_tournament(self):
        t.copy_set(101, 1, OTHER, 'B')
        mid = add_board(self.ch1, OTHER, 'B')
        with self.changed([link(101)]):
            await self.cog.sync_code(CODE)
        self.assertIn("이 채널을 다른 조에 연결해", edited_text(self.ch1, mid))
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
        self.assertIn("대회가 지워져", edited_text(self.ch1, mid))
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
        self.assertIn("연결을 해제해", edited_text(self.ch2, mid))

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
        self.assertIn("대회가 지워져", edited_text(self.ch1, mid))


if __name__ == '__main__':
    unittest.main()
