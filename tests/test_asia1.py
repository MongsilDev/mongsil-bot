import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import helpers  # noqa: F401

from utils import labs_cut, rank_helpers

TOP = [{'nickname': 'a', 'rank': 1, 'mmr': 7000}, {'nickname': 'b', 'rank': 2, 'mmr': 6900}]


class Asia1Test(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name, 'asia1.json')
        p = mock.patch.object(labs_cut, 'ASIA', str(self.path))
        p.start()
        self.addCleanup(p.stop)
        labs_cut._cache.clear()

    def write(self, **extra):
        data = {'at': time.time() * 1000, 'season': 43, 'active': True, 'topRanks': TOP, **extra}
        self.path.write_text(json.dumps(data))
        labs_cut._cache.clear()

    def test_place_only_when_official_rank_is_zero(self):
        self.write()
        self.assertEqual(rank_helpers.server_place({'serverRank': 0, 'serverCode': 10, 'nickname': 'b'}, 43), 2)
        self.assertEqual(rank_helpers.server_place({'serverRank': 5, 'serverCode': 10, 'nickname': 'b'}, 43), 5)
        self.assertEqual(rank_helpers.server_place({'serverRank': 0, 'serverCode': 12, 'nickname': 'b'}, 43), 0)
        self.assertEqual(rank_helpers.server_place({'serverRank': 0, 'serverCode': 10, 'nickname': 'zz'}, 43), 0)
        self.assertEqual(rank_helpers.server_place(None, 43), 0)

    def test_inactive_or_old_file_is_ignored(self):
        self.write(active=False)
        self.assertIsNone(labs_cut.asia(43))
        self.write(at=(time.time() - labs_cut.ASIA_FRESH - 5) * 1000)
        self.assertIsNone(labs_cut.asia(43))
        self.assertEqual(rank_helpers.server_place({'serverRank': 0, 'serverCode': 10, 'nickname': 'b'}, 43), 0)
        self.write()
        self.assertIsNone(labs_cut.asia(44))
        self.assertEqual(labs_cut.asia(43)['topRanks'], TOP)

    async def test_ranking_uses_rebuilt_board_without_calling_api(self):
        self.write()
        client = mock.Mock()
        client.api_client.get = mock.AsyncMock()
        self.assertEqual(await rank_helpers.fetch_ranking_data(client, 43), TOP)
        client.api_client.get.assert_not_called()
        self.write(active=False)
        client.api_client.get = mock.AsyncMock(return_value={'code': 200, 'topRanks': TOP[:1]})
        self.assertEqual(await rank_helpers.fetch_ranking_data(client, 43), TOP[:1])

    def test_tier_text_uses_rebuilt_place(self):
        self.write()
        tier, place = rank_helpers.tier_and_place({'mmr': 7000, 'rank': 1, 'seasonId': 43}, {'serverRank': 0, 'serverCode': 10, 'nickname': 'a'})
        self.assertEqual(place, '아시아1 1위')
        _, place = rank_helpers.tier_and_place({'mmr': 7000, 'rank': 1, 'seasonId': 44}, {'serverRank': 0, 'serverCode': 10, 'nickname': 'a'})
        self.assertEqual(place, '통합 1위')

    def test_rebuilt_file_replaced_at_same_address_is_reread(self):
        self.write()
        self.assertEqual(labs_cut.asia_place('b', 43), 2)
        self.write(topRanks=[{'nickname': 'b', 'rank': 1, 'mmr': 7100}, {'nickname': 'x', 'mmr': 1}])
        self.assertEqual(labs_cut.asia_place('b', 43), 1)
        self.assertEqual(labs_cut.asia_place('x', 43), 0)
        self.write(at='bad')
        self.assertIsNone(labs_cut.asia(43))


if __name__ == '__main__':
    unittest.main()
