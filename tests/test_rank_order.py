import unittest
from unittest import mock

import helpers  # noqa: F401

from commands import rating
from utils import rank_history


def rows():
    top = [{'nickname': f'p{i}', 'rank': i, 'mmr': 9000 - i} for i in range(1, 1001)]
    top.append({'nickname': 'late', 'rank': 1001, 'mmr': 8914})
    return top


class RankOrderTest(unittest.IsolatedAsyncioTestCase):
    def test_active_cut_ignores_late_account(self):
        book = {r['nickname']: [r['mmr']] + [0] * 6 for r in rows()}
        dormant = lambda p, mmr, *_: 7000 if mmr < 8500 and mmr % 2 else mmr
        with mock.patch.object(rank_history, 'player_book', return_value=book), \
                mock.patch.object(rank_history, 'projected_rp', dormant):
            value, _, estimated = rank_history.active_cut(41, rows(), 1000)
        self.assertTrue(estimated)
        self.assertLessEqual(value, 8000)

    async def test_board_cut_ignores_late_account(self):
        top = rows()
        top[999], top[1000] = {**top[1000], 'rank': 1000}, {**top[999], 'rank': 1001}
        with mock.patch.object(rating, 'fetch_ranking_data', mock.AsyncMock(return_value=top)), \
                mock.patch.object(rating, 'get_season_info', mock.AsyncMock(return_value=None)), \
                mock.patch.object(rank_history, 'record_cuts'):
            rank_300, rank_1000 = await rating.fetch_rating_info(mock.Mock(), 41)
        self.assertEqual((rank_300['mmr'], rank_1000['mmr'], rank_1000['rank']), (8701, 8001, 999))


if __name__ == '__main__':
    unittest.main()
