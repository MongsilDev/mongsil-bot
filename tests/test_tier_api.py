import json
import os
import unittest
from unittest import mock

import helpers  # noqa: F401

from aiohttp.test_utils import make_mocked_request

from commands import rank
from utils.errors import NotFoundError
from web import server

GAMES = {'userGames': [
    {'matchingMode': 2, 'seasonId': 41, 'mmrAfter': 0},
    {'matchingMode': 3, 'seasonId': 41, 'mmrAfter': 6842, 'mmrGain': 38, 'gameRank': 2, 'startDtm': '2026-11-09T01:00:00.000+0900'},
    {'matchingMode': 3, 'seasonId': 40, 'mmrAfter': 6500, 'mmrGain': 10, 'gameRank': 1},
    {'matchingMode': 3, 'seasonId': 41, 'mmrAfter': 6804, 'mmrGain': -12, 'gameRank': 7},
]}


def fake_client(uid='u1', games=GAMES):
    client = mock.Mock()
    client.get_user_nickname = mock.AsyncMock(return_value=uid)
    client.api_client.get = mock.AsyncMock(return_value=games)
    return client


class TierSnapshotTest(unittest.IsolatedAsyncioTestCase):
    def patches(self, stats, user_rank):
        return (mock.patch.object(rank, 'get_ranked_season', mock.AsyncMock(return_value=(41, '시즌 9'))),
                mock.patch.object(rank, 'fetch_user_stats_solo', mock.AsyncMock(side_effect=stats) if isinstance(stats, Exception)
                                  else mock.AsyncMock(return_value=stats)),
                mock.patch.object(rank, 'fetch_user_rank', mock.AsyncMock(return_value=user_rank)))

    async def test_tier_rp_and_last_gain(self):
        a, b, c = self.patches({'nickname': 'Foo', 'mmr': 6842, 'rank': 5000, 'rankSize': 100000},
                               {'serverRank': 1284, 'serverCode': 10})
        with a, b as stats, c as user_rank:
            data = await rank.tier_snapshot(fake_client(), 'foo')
        self.assertEqual(data['nickname'], 'Foo')
        self.assertEqual(data['tier'], '메테오라이트 3')
        self.assertEqual(data['mmr'], 6842)
        self.assertEqual(data['serverRank'], 1284)
        self.assertEqual(data['last']['gain'], 38)
        self.assertEqual(data['recent'], [2, 7])
        self.assertEqual(stats.call_args.kwargs['ttl'], 60)
        self.assertEqual(user_rank.call_args.kwargs['ttl'], 60)

    async def test_unknown_nickname(self):
        a, b, c = self.patches({}, None)
        with a, b, c:
            self.assertIsNone(await rank.tier_snapshot(fake_client(uid=None), 'nobody'))

    async def test_no_ranked_games_this_season(self):
        a, b, c = self.patches(NotFoundError('유저 통계 없음', '랭크 게임 기록이 있는 유저만 조회할 수 있습니다.'), None)
        with a, b, c:
            data = await rank.tier_snapshot(fake_client(), 'newbie')
        self.assertEqual(data['tier'], '언랭크')
        self.assertIsNone(data['last'])
        self.assertEqual(data['recent'], [])


class TierApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        server._tier_cache.clear()
        server._tier_locks.clear()
        server._tier_fetches.clear()
        self.env = mock.patch.dict(os.environ, {'LABS_BOT_TOKEN': 'tok'})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def request(self, n='Foo', token='tok'):
        return make_mocked_request('GET', f'/api/tier?n={n}', headers={'x-bot-token': token})

    async def test_wrong_token_is_hidden(self):
        r = await server.api_tier(self.request(token='bad'))
        self.assertEqual(r.status, 404)
        self.assertIsNone(r.body)

    async def test_bad_nickname(self):
        r = await server.api_tier(self.request(n='a b c'))
        self.assertEqual(r.status, 400)

    async def test_cached_for_a_minute(self):
        snap = mock.AsyncMock(return_value={'nickname': 'Foo', 'tier': '골드 1', 'mmr': 3500})
        with mock.patch.object(rank, 'tier_snapshot', snap):
            first = await server.api_tier(self.request())
            second = await server.api_tier(self.request(n='foo'))
        self.assertEqual(first.status, 200)
        self.assertEqual(json.loads(second.body)['tier'], '골드 1')
        self.assertEqual(snap.await_count, 1)

    async def test_not_found_is_remembered(self):
        snap = mock.AsyncMock(return_value=None)
        with mock.patch.object(rank, 'tier_snapshot', snap):
            r1 = await server.api_tier(self.request(n='Nobody'))
            r2 = await server.api_tier(self.request(n='Nobody'))
        self.assertEqual((r1.status, r2.status), (404, 404))
        self.assertEqual(snap.await_count, 1)

    async def test_fetches_per_minute_are_capped(self):
        snap = mock.AsyncMock(return_value={'nickname': 'x', 'tier': '골드 1', 'mmr': 3500})
        with mock.patch.object(rank, 'tier_snapshot', snap):
            statuses = [(await server.api_tier(self.request(n=f'name{i}'))).status for i in range(server.TIER_FETCH_PER_MIN + 1)]
        self.assertEqual(statuses[-1], 429)
        self.assertEqual(snap.await_count, server.TIER_FETCH_PER_MIN)


if __name__ == '__main__':
    unittest.main()
