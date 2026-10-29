import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import CODE, FakeChannel, FakeClient, add_board, board, reset, t, text_of

import discord


class NoticeTest(unittest.IsolatedAsyncioTestCase):
    def test_single_group_points(self):
        self.assertEqual(t.notice_text(board(played=1)), "### 테스트 대회\n-# 포인트제 | 4라운드")
        self.assertEqual(t.notice_text(board(played=1), image=True), "")

    def test_next_round_before_first_result(self):
        nxt = {'roundNo': 1, 'main': '비', 'sub': None, 'bans': []}
        self.assertEqual(t.notice_text(board(next=nxt), image=True), "-# 1라운드 | 날씨 비")
        self.assertNotIn("라운드 |", t.notice_text(board(played=1, next={**nxt, 'roundNo': 2}), image=True))
        # 날씨도 금지 실험체도 없으면 라운드 번호만 있는 줄을 두지 않음
        self.assertEqual(t.next_lines({**nxt, 'main': None}), [])

    def test_ban_faces(self):
        with mock.patch.dict(t.app_emojis.EMOJI, {'Jackie': '<:Jackie:1>', 'Aya': '<:Aya:2>'}):
            both = t.next_lines({'roundNo': 2, 'main': '맑음', 'sub': '비', 'bans': ['재키', '아야']})
            mixed = t.next_lines({'roundNo': 2, 'main': None, 'sub': None, 'bans': ['재키', '없는 이름']})
            many = t.next_lines({'roundNo': 2, 'main': '맑음', 'sub': '비', 'bans': ['재키', '아야', '없는 이름', '긴 이름 하나']})
        self.assertEqual(both, ["-# 2라운드 | 날씨 맑음, 비 | 금지 <:Jackie:1> 재키 <:Aya:2> 아야"])
        self.assertEqual(mixed, ["-# 2라운드 | 금지 <:Jackie:1> 재키, 없는 이름"])
        self.assertEqual(many, ["-# 2라운드 | 날씨 맑음, 비", "-# 금지 <:Jackie:1> 재키, <:Aya:2> 아야, 없는 이름, 긴 이름 하나"])

    def test_many_groups_other_mode(self):
        b = board(group='B', groups=3, mode='checkpoint', played=2,
                  next={'roundNo': 3, 'main': '비', 'sub': None, 'bans': ['재키']})
        self.assertEqual(t.notice_text(b), "### 테스트 대회 B조\n-# 체크포인트")
        self.assertEqual(t.csv_text(b), "이 채널에 올린 결과 CSV는 B조 순위에 반영됩니다.")

    def test_stopped_states(self):
        b = board(group='C', groups=2)
        for state in ('unlinked', 'moved', 'replaced'):
            self.assertEqual(t.notice_text(b, state, image=True), "-# 이 채널에는 더 이상 순위를 올리지 않습니다.")
        self.assertEqual(t.notice_text(b, 'nogroup'), "### 테스트 대회 C조\n-# 삭제된 조입니다.")
        self.assertEqual(t.notice_text(b, 'ended'), "### 테스트 대회 C조\n-# 경기가 끝났습니다.")
        # 끝난 대회는 최종 순위 메시지가 따로 있어 이미지만 남김
        self.assertEqual(t.notice_text(b, 'ended', image=True), "")
        gone = t.notice_view(b, 'gone', image=True)
        self.assertEqual(text_of(gone), "-# 삭제된 대회입니다.")
        self.assertFalse(any(isinstance(i, discord.ui.Button) for i in gone.walk_children()))

    def test_replay_codes(self):
        replays = [{'roundNo': 1, 'gameId': 12345678}, {'roundNo': 2, 'gameId': 23456789}]
        one = {**board(ended=True), 'name': '몽실*컵', 'replays': replays}
        self.assertEqual(t.replay_text(one), "### 리플레이 코드\n-# 몽실\\*컵\n1R `12345678`\n2R `23456789`")
        many = {**board(group='B', groups=3, ended=True), 'replays': replays[:1]}
        self.assertEqual(t.replay_text(many), "### 리플레이 코드 B조\n-# 테스트 대회\n1R `12345678`")
        buttons = [i for i in t.replay_view(one).walk_children() if isinstance(i, discord.ui.Button)]
        self.assertEqual([(b.label, b.url) for b in buttons], [("순위표", one['url'])])

    async def test_only_rank_button(self):
        for state in ('on', 'unlinked'):
            view = t.notice_view(board(), state)
            buttons = [i for i in view.walk_children() if isinstance(i, discord.ui.Button)]
            self.assertEqual([(b.label, b.style, b.emoji) for b in buttons],
                             [("순위표", discord.ButtonStyle.link, None)])

    def test_result_titles_game_rank(self):
        r = {'status': 'added', 'name': '테스트 대회', 'group': 'A', 'groupCount': 2, 'round': 1,
             'url': f'https://labs.example/er/t/{CODE}/a',
             'ranking': [{'rank': 1, 'team': '팀1', 'score': 13}, {'rank': 2, 'team': '팀2', 'score': 20}]}
        self.assertEqual(text_of(t.result_view(r)), "### 테스트 대회 A조 1라운드 게임 순위\n1위 팀1 13점\n2위 팀2 20점")

    async def test_result_button_without_emoji(self):
        r = {'status': 'duplicate', 'message': '이미 올린 결과입니다.', 'url': 'https://labs.example/x'}
        buttons = [i for i in t.result_view(r).walk_children() if isinstance(i, discord.ui.Button)]
        self.assertEqual([(b.label, b.emoji) for b in buttons], [("순위표", None)])

    def test_already_linked(self):
        self.assertEqual(t.already_text(board()), "이 채널은 이미 테스트 대회에 연결돼 있습니다.")
        self.assertEqual(t.already_text(board(group='B', groups=2)), "이 채널은 이미 테스트 대회 B조에 연결돼 있습니다.")


def attachment(aid=77, filename='game.csv'):
    return SimpleNamespace(id=aid, filename=filename, size=10, read=mock.AsyncMock(return_value=b'a,b'))


def csv_message(channel, name='올린사람' * 10, attachments=None, content='', stickers=()):
    channel = channel if isinstance(channel, FakeChannel) else FakeChannel(channel)
    return SimpleNamespace(id=500, guild=channel.guild, author=SimpleNamespace(bot=False, id=42, display_name=name),
                           channel=channel, attachments=attachments or [attachment()],
                           content=content, stickers=list(stickers),
                           reply=mock.AsyncMock(), delete=mock.AsyncMock())


def added(**kw):
    return {'status': 'added', 'code': CODE, 'group': 'A', 'groupCount': 1, 'name': '테스트 대회', 'round': 1,
            'ranking': [], 'url': 'https://labs.example/x', **kw}


NOT_FOUND = discord.NotFound(SimpleNamespace(status=404, reason=''), '')


class OnMessageTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        reset()
        self.ch = FakeChannel(101)
        self.cog = t.Tournament(FakeClient(self.ch))

    def labs(self, *results, b=None):
        """relay 응답과 순위표 조회를 바꿔 끼움"""
        return mock.patch.multiple(t, relay=mock.AsyncMock(side_effect=list(results)),
                                   fetch_board=mock.AsyncMock(return_value=b or board(played=1)),
                                   fetch_image=mock.AsyncMock(return_value=b'png'))

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
        self.assertEqual(t._kept[77][4], '처음올린사람')
        view = message.reply.call_args.kwargs['view']
        self.assertTrue(any(isinstance(i, t.RelayRetryButton) for i in view.walk_children()))

    async def test_added_posts_image_then_deletes(self):
        t.copy_set(101, 1, CODE, 'A')
        message = csv_message(self.ch)
        message.delete.side_effect = lambda: self.assertEqual(len(self.ch.sent), 1)
        with self.labs(added()):
            await self.cog.on_message(message)
        message.reply.assert_not_called()
        self.assertEqual(len(self.ch.sent), 1)
        self.assertIn('tournament.png', [f.filename for f in self.ch.sent[0][1]['files']])
        message.delete.assert_awaited_once()

    async def test_one_failed_of_two(self):
        t.copy_set(101, 1, CODE, 'A')
        message = csv_message(self.ch, attachments=[attachment(77), attachment(78, 'b.csv')])
        dup = {'status': 'duplicate', 'message': '이미 올린 결과입니다.', 'url': 'https://labs.example/x'}
        with self.labs(added(), dup):
            await self.cog.on_message(message)
        message.reply.assert_called_once()
        self.assertEqual(text_of(message.reply.call_args.kwargs['view']), '이미 올린 결과입니다.')
        self.assertEqual(len(self.ch.sent), 1)
        message.delete.assert_not_called()

    async def test_other_attachment_keeps_message(self):
        t.copy_set(101, 1, CODE, 'A')
        message = csv_message(self.ch, attachments=[attachment(), attachment(78, 'shot.png')])
        with self.labs(added()):
            await self.cog.on_message(message)
        self.assertEqual(len(self.ch.sent), 1)
        message.delete.assert_not_called()

    async def test_no_manage_permission_keeps_message(self):
        perms = discord.Permissions.all()
        perms.manage_messages = False
        self.ch.perms = perms
        t.copy_set(101, 1, CODE, 'A')
        message = csv_message(self.ch)
        with self.labs(added()):
            await self.cog.on_message(message)
        message.reply.assert_not_called()
        self.assertEqual(len(self.ch.sent), 1)
        message.delete.assert_not_called()

    async def test_image_failed_keeps_message(self):
        t.copy_set(101, 1, CODE, 'A')
        message = csv_message(self.ch)
        with self.labs(added()), mock.patch.object(
                t, 'fetch_board', mock.AsyncMock(side_effect=t.ServiceDownError('Labs 응답 500', t.DOWN))):
            await self.cog.on_message(message)
        self.assertEqual(self.ch.sent, [])
        message.reply.assert_called_once()
        self.assertEqual(text_of(message.reply.call_args.kwargs['view']), "### 테스트 대회 1라운드 게임 순위")
        message.delete.assert_not_called()

    async def test_image_failed_replies_each_rank(self):
        t.copy_set(101, 1, CODE, 'A')
        message = csv_message(self.ch, attachments=[attachment(77), attachment(78, 'b.csv')])
        r1 = added(ranking=[{'rank': 1, 'team': '팀1', 'score': 13}])
        r2 = added(round=2, ranking=[{'rank': 1, 'team': '팀2', 'score': 9}])
        with self.labs(r1, r2), mock.patch.object(self.cog, 'show_board', mock.AsyncMock(return_value=False)):
            await self.cog.on_message(message)
        self.assertEqual([text_of(c.kwargs['view']) for c in message.reply.call_args_list],
                         ["### 테스트 대회 1라운드 게임 순위\n1위 팀1 13점", "### 테스트 대회 2라운드 게임 순위\n1위 팀2 9점"])
        message.delete.assert_not_called()

    async def test_text_or_sticker_keeps_message(self):
        t.copy_set(101, 1, CODE, 'A')
        for kw in ({'content': '1라운드 결과'}, {'stickers': [object()]}):
            self.ch.sent.clear()
            message = csv_message(self.ch, **kw)
            with self.labs(added()):
                await self.cog.on_message(message)
            message.reply.assert_not_called()
            self.assertEqual(len(self.ch.sent), 1)
            message.delete.assert_not_called()

    async def test_text_with_retry_not_pending(self):
        t.copy_set(101, 1, CODE, 'A')
        message = csv_message(self.ch, attachments=[attachment(77), attachment(78, 'b.csv')], content='결과')
        with self.labs(added(), None):
            await self.cog.on_message(message)
        self.assertIn(78, t._kept)
        self.assertEqual(t._pending, {})

    async def test_already_deleted_is_quiet(self):
        t.copy_set(101, 1, CODE, 'A')
        message = csv_message(self.ch)
        message.delete.side_effect = NOT_FOUND
        with self.labs(added()):
            await self.cog.on_message(message)
        message.delete.assert_awaited_once()

    async def test_retry_added_deletes_reply_and_original(self):
        t.copy_set(101, 1, CODE, 'A')
        message = csv_message(self.ch, attachments=[attachment(77), attachment(78, 'b.csv')])
        with self.labs(added(), None):
            await self.cog.on_message(message)
        self.assertEqual(len(self.ch.sent), 1)
        message.delete.assert_not_called()
        self.assertEqual(t._pending, {500: {78}})

        interaction = self.retry_interaction()
        with self.labs(added(round=2), b=board(played=2)):
            await t.RelayRetryButton(78).callback(interaction)
        self.assertEqual(len(self.ch.sent), 2)
        interaction.message.delete.assert_awaited_once()
        interaction.edit_original_response.assert_not_called()
        self.assertEqual(self.ch.deleted, [500])
        self.assertEqual(t._pending, {})

    def retry_interaction(self):
        client = self.cog.client
        client.get_cog = lambda name: self.cog
        return SimpleNamespace(
            client=client, user=SimpleNamespace(id=4242), app_permissions=self.ch.perms,
            response=SimpleNamespace(defer=mock.AsyncMock()), edit_original_response=mock.AsyncMock(),
            message=SimpleNamespace(delete=mock.AsyncMock()))

    async def test_retry_image_failed_edits_reply(self):
        t.copy_set(101, 1, CODE, 'A')
        message = csv_message(self.ch, attachments=[attachment(77), attachment(78, 'b.csv')])
        with self.labs(added(), None):
            await self.cog.on_message(message)
        interaction = self.retry_interaction()
        r = added(round=2, ranking=[{'rank': 1, 'team': '팀1', 'score': 13}])
        with self.labs(r), mock.patch.object(self.cog, 'show_board', mock.AsyncMock(return_value=False)):
            await t.RelayRetryButton(78).callback(interaction)
        interaction.message.delete.assert_not_called()
        view = interaction.edit_original_response.call_args.kwargs['view']
        self.assertEqual(text_of(view), "### 테스트 대회 2라운드 게임 순위\n1위 팀1 13점")
        self.assertEqual(self.ch.deleted, [])
        self.assertEqual(t._pending, {})

    async def test_keep_during_retry_still_deletes(self):
        t.copy_set(101, 1, CODE, 'A')
        message = csv_message(self.ch, attachments=[attachment(77), attachment(78, 'b.csv')])
        with self.labs(added(), None):
            await self.cog.on_message(message)

        async def relay(*args):
            # 다시 보내는 사이 다른 메시지의 CSV를 보관함
            t.keep_file(90, 101, 600, 43, '다른사람', 'c.csv', b'a,b')
            return added(round=2)

        interaction = self.retry_interaction()
        with self.labs(b=board(played=2)), mock.patch.object(t, 'relay', relay):
            await t.RelayRetryButton(78).callback(interaction)
        interaction.message.delete.assert_awaited_once()
        self.assertEqual(self.ch.deleted, [500])
        self.assertEqual(t._pending, {})
        self.assertEqual(t._relaying, set())

    async def test_retry_during_image_still_deletes(self):
        t.copy_set(101, 1, CODE, 'A')
        message = csv_message(self.ch, attachments=[attachment(77), attachment(78, 'b.csv')])
        interaction = self.retry_interaction()
        calls = []

        async def show_board(*args):
            # 첫 이미지를 올리는 사이 다시 보내기가 끝남
            calls.append(args)
            if len(calls) == 1:
                with mock.patch.object(t, 'relay', mock.AsyncMock(return_value=added(round=2))):
                    await t.RelayRetryButton(78).callback(interaction)
            return True

        with self.labs(added(), None), mock.patch.object(self.cog, 'show_board', show_board):
            await self.cog.on_message(message)
        self.assertEqual(len(calls), 2)
        interaction.message.delete.assert_awaited_once()
        self.assertEqual(self.ch.deleted, [500])
        self.assertEqual(t._pending, {})

    async def test_retry_during_other_relay_keeps_message(self):
        t.copy_set(101, 1, CODE, 'A')
        message = csv_message(self.ch, attachments=[attachment(77, 'a.csv'), attachment(78, 'b.csv'), attachment(79, 'c.csv')])
        interaction = self.retry_interaction()
        results = [added(), None]

        async def relay(*args):
            if 'c.csv' in args:
                # 셋째 파일을 보내는 사이 둘째 파일의 다시 보내기가 끝남
                with mock.patch.object(t, 'relay', mock.AsyncMock(return_value=added(round=2))):
                    await t.RelayRetryButton(78).callback(interaction)
                return added(round=3)
            return results.pop(0)

        with mock.patch.object(t, 'relay', relay), mock.patch.object(self.cog, 'show_board', mock.AsyncMock(return_value=True)):
            await self.cog.on_message(message)
        self.assertEqual(self.ch.deleted, [])
        message.delete.assert_not_called()
        self.assertEqual(t._pending, {})

    async def test_waits_for_running_refresh(self):
        t.copy_set(101, 1, CODE, 'A')
        mid = add_board(self.ch)
        gate, calls = asyncio.Event(), []

        async def tick(row):
            calls.append(row['message_id'])
            if len(calls) == 1:
                await gate.wait()
                await self.ch.send(view=None, files=[])
                key = (101, CODE, 'A')
                self.cog._posted[key] = self.cog._posted.get(key, 0) + 1

        message = csv_message(self.ch)
        with mock.patch.object(self.cog, 'tick', tick), self.labs(added()):
            running = asyncio.create_task(self.cog.refresh(mid))
            await asyncio.sleep(0)
            handling = asyncio.create_task(self.cog.on_message(message))
            for _ in range(5):
                await asyncio.sleep(0)
            message.delete.assert_not_called()
            gate.set()
            await asyncio.gather(running, handling)
        self.assertEqual(calls, [mid, mid])
        self.assertEqual(len(self.ch.sent), 1)
        message.delete.assert_awaited_once()

    def test_reupload_phrases(self):
        self.assertTrue(t.DOWNLOAD_FAILED.startswith("파일을 받지 못했습니다."))
        self.assertTrue(t.KEEP_EXPIRED.startswith("보관한 파일이 없어"))


if __name__ == '__main__':
    unittest.main()
