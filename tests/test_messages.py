import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import CODE, FakeChannel, FakeClient, board, reset, t, text_of

import discord


class NoticeTest(unittest.IsolatedAsyncioTestCase):
    def test_single_group_points(self):
        self.assertEqual(t.notice_text(board(played=1)),
                         "### 테스트 대회\n-# 포인트제 | 1/4라운드\n"
                         "-# 이 채널에 올린 게임 결과 CSV는 순위에 들어갑니다.")

    def test_many_groups_other_mode(self):
        b = board(group='B', groups=3, mode='checkpoint', played=2,
                  next={'roundNo': 3, 'main': '비', 'sub': None, 'bans': ['재키']})
        self.assertEqual(t.notice_text(b),
                         "### 테스트 대회 B조\n-# 체크포인트 | 2라운드\n-# 다음 3라운드 | 날씨 비\n-# 금지 실험체 재키\n"
                         "-# 이 채널에 올린 게임 결과 CSV는 B조 순위에 들어갑니다.")

    def test_stopped_states(self):
        b = board(group='C', groups=2)
        self.assertEqual(t.notice_text(b, 'unlinked'), "### 테스트 대회 C조\n-# 연결을 해제해 자동 갱신을 멈췄습니다.")
        self.assertEqual(t.notice_text(b, 'moved'), "### 테스트 대회 C조\n-# 다른 채널로 연결을 옮겨 자동 갱신을 멈췄습니다.")
        self.assertEqual(t.notice_text(b, 'replaced'),
                         "### 테스트 대회 C조\n-# 이 채널을 다른 조에 연결해 자동 갱신을 멈췄습니다.")
        self.assertEqual(t.notice_text(b, 'nogroup'), "### 테스트 대회 C조\n-# C조가 없어져 자동 갱신을 멈췄습니다.")
        self.assertIn("대회가 끝나", t.notice_text(b, 'ended'))
        self.assertIn("대회가 지워져", t.notice_text(b, 'gone'))

    async def test_only_rank_button(self):
        for state in ('on', 'unlinked'):
            view = t.notice_view(board(), state)
            buttons = [i for i in view.walk_children() if isinstance(i, discord.ui.Button)]
            self.assertEqual([(b.label, b.style) for b in buttons], [("순위표", discord.ButtonStyle.link)])

    def test_result_titles_game_rank(self):
        r = {'status': 'added', 'name': '테스트 대회', 'group': 'A', 'groupCount': 2, 'round': 1,
             'url': f'https://labs.example/er/t/{CODE}/a',
             'ranking': [{'rank': 1, 'team': '팀1', 'score': 13}, {'rank': 2, 'team': '팀2', 'score': 20}]}
        self.assertEqual(text_of(t.result_view(r)), "### 테스트 대회 A조 1라운드 게임 순위\n1위 팀1 13점\n2위 팀2 20점")

    def test_already_linked(self):
        self.assertEqual(t.already_text(board()), "이 채널은 이미 테스트 대회에 연결돼 있습니다.")
        self.assertEqual(t.already_text(board(group='B', groups=2)), "이 채널은 이미 테스트 대회 B조에 연결돼 있습니다.")


def csv_message(channel_id, name='올린사람' * 10):
    attachment = SimpleNamespace(id=77, filename='game.csv', size=10, read=mock.AsyncMock(return_value=b'a,b'))
    return SimpleNamespace(guild=object(), author=SimpleNamespace(bot=False, id=42, display_name=name),
                           channel=SimpleNamespace(id=channel_id), attachments=[attachment],
                           reply=mock.AsyncMock())


class OnMessageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset()
        self.cog = t.Tournament(FakeClient(FakeChannel(101)))

    async def test_unlinked_channel_is_silent(self):
        message = csv_message(101)
        with mock.patch.object(t, 'relay', mock.AsyncMock()) as relay:
            await self.cog.on_message(message)
        relay.assert_not_called()
        message.reply.assert_not_called()

    async def test_linked_channel_relays_anyone(self):
        t.copy_set(101, 1, CODE, 'A')
        message = csv_message(101)
        result = {'status': 'duplicate', 'message': '이미 올린 결과입니다.', 'url': 'https://labs.example/x'}
        with mock.patch.object(t, 'relay', mock.AsyncMock(return_value=result)) as relay:
            await self.cog.on_message(message)
        channel_id, actor, name, file_name, data = relay.call_args.args
        self.assertEqual((channel_id, actor, file_name, data), (101, 42, 'game.csv', b'a,b'))
        self.assertEqual(name, message.author.display_name[:32])
        message.reply.assert_called_once()

    async def test_retry_keeps_uploader_name(self):
        t.copy_set(101, 1, CODE, 'A')
        message = csv_message(101, '처음올린사람')
        with mock.patch.object(t, 'relay', mock.AsyncMock(return_value=None)):
            await self.cog.on_message(message)
        self.assertEqual(t._kept[77][3], '처음올린사람')
        view = message.reply.call_args.kwargs['view']
        self.assertTrue(any(isinstance(i, t.RelayRetryButton) for i in view.walk_children()))

    async def test_added_refreshes_boards(self):
        t.copy_set(101, 1, CODE, 'A')
        result = {'status': 'added', 'code': CODE, 'group': 'A', 'groupCount': 1, 'name': '테스트 대회', 'round': 1,
                  'ranking': [], 'url': 'https://labs.example/x'}
        with mock.patch.object(t, 'relay', mock.AsyncMock(return_value=result)), \
                mock.patch.object(self.cog, 'refresh_code', mock.AsyncMock()) as refresh:
            await self.cog.on_message(csv_message(101))
            for task in list(self.cog._tasks):
                await task
        refresh.assert_awaited_once_with(CODE)

    def test_reupload_phrases(self):
        self.assertTrue(t.DOWNLOAD_FAILED.startswith("파일을 받지 못했습니다."))
        self.assertTrue(t.KEEP_EXPIRED.startswith("보관한 파일이 없어"))


if __name__ == '__main__':
    unittest.main()
