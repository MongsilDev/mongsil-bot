import unittest
from datetime import datetime
from unittest import mock

import helpers  # noqa: F401

from commands import season
from commands.rating import season_end_for
from utils import rank_history
from utils.rank_history import KST

S41 = {'seasonID': 41, 'seasonName': 'Season21', 'seasonStart': '2026-08-13 12:00:00', 'seasonEnd': '2026-11-12 09:00:00'}
PRE = {'seasonID': 40, 'seasonName': 'Pre-Season21', 'seasonStart': '2026-08-06 12:00:00', 'seasonEnd': '2026-08-13 12:00:00', 'isCurrent': 0}


def listing(*rows):
    return {'code': 200, 'data': [PRE, *rows]}


class SeasonChangeTest(unittest.IsolatedAsyncioTestCase):
    """11-12 09:00 정규 시즌 12가 끝나고 Pre-Season22 행이 늦게 생겨도 랭크는 시즌 41을 씀"""

    async def ranked(self, data):
        season._season_cache = season._season_cache_time = None
        with mock.patch('utils.api_client.api_client.get', mock.AsyncMock(return_value=data)):
            return await season.get_ranked_season(), await season.get_season_info()

    async def test_ended_but_still_current(self):
        (sid, name), info = await self.ranked(listing({**S41, 'isCurrent': 1}))
        self.assertEqual((sid, name), (41, '정규 시즌 12'))
        self.assertEqual(season_end_for(info, sid), datetime(2026, 11, 12, 9, tzinfo=KST))

    async def test_no_current_row_yet(self):
        (sid, _), _ = await self.ranked(listing({**S41, 'isCurrent': 0}))
        self.assertEqual(sid, 41)

    async def test_pre_season_row_without_end(self):
        pre = {'seasonID': 42, 'seasonName': 'Pre-Season22', 'seasonStart': '2026-11-12 12:00:00', 'seasonEnd': '', 'isCurrent': 1}
        (sid, name), info = await self.ranked(listing({**S41, 'isCurrent': 0}, pre))
        self.assertEqual((sid, name), (41, '정규 시즌 12'))
        self.assertIsNone(season_end_for(info, sid))
        # /시즌이 오류 대신 프리 시즌 1주를 보여 줌
        self.assertEqual(info.end_date, datetime(2026, 11, 19, 12, tzinfo=KST))

    async def test_pre_season_row_not_current_yet(self):
        pre = {'seasonID': 42, 'seasonName': 'Pre-Season22', 'seasonStart': '2026-11-12 12:00:00', 'seasonEnd': '2026-11-19 12:00:00', 'isCurrent': 0}
        (sid, _), _ = await self.ranked(listing({**S41, 'isCurrent': 1}, pre))
        self.assertEqual(sid, 41)

    async def test_pre_season_current(self):
        pre = {'seasonID': 42, 'seasonName': 'Pre-Season22', 'seasonStart': '2026-11-12 12:00:00', 'seasonEnd': '2026-11-19 12:00:00', 'isCurrent': 1}
        (sid, _), info = await self.ranked(listing({**S41, 'isCurrent': 0}, pre))
        self.assertEqual(sid, 41)
        self.assertEqual(info.name, '프리 시즌 13')
        self.assertIsNone(season_end_for(info, sid))


class EndedSeasonDecayTest(unittest.TestCase):
    end = datetime(2026, 11, 12, 9, tzinfo=KST).timestamp()

    def test_no_decay_added_after_end(self):
        # 11-01부터 쉰 계정. 종료 뒤에는 순위표 RP가 그대로 최종값
        last = datetime(2026, 11, 1, 12, tzinfo=KST).timestamp()
        p = [0] * 8
        with mock.patch.object(rank_history, 'last_active', return_value=last):
            for day in (13, 14, 20):
                now = datetime(2026, 11, day, 16, tzinfo=KST).timestamp()
                self.assertLessEqual(rank_history.projected_rp(p, 9000, now, self.end), 9000, day)


if __name__ == '__main__':
    unittest.main()
