import unittest
from datetime import datetime

import helpers  # noqa: F401

from utils import rank_history
from utils.rank_history import KST


def at(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=KST).timestamp()


class DecayTicksTest(unittest.TestCase):
    until = at(20, 16)

    def test_game_after_five_counts_for_that_day(self):
        before, after = rank_history.decay_ticks(at(2, 4, 59), self.until), rank_history.decay_ticks(at(2, 6), self.until)
        self.assertEqual(before - after, 1)
        self.assertEqual(after, rank_history.decay_ticks(at(3, 4, 59), self.until))

    def test_five_to_nine_matches_rest_of_day(self):
        self.assertEqual(rank_history.decay_ticks(at(2, 6), self.until), rank_history.decay_ticks(at(2, 23), self.until))
        self.assertEqual(rank_history.decay_ticks(at(2, 5), self.until), rank_history.decay_ticks(at(2, 8, 59), self.until))

    def test_counts_after_grace(self):
        # 11-03~11-20 15시 18번 가운데 11-12 패치 날을 빼고 유예 12를 쓴 나머지
        self.assertEqual(rank_history.decay_ticks(at(2, 4, 59), self.until), 5)


class ForecastSurgeTest(unittest.TestCase):
    end = datetime(2026, 11, 12, 9, tzinfo=KST)
    flat = [(datetime(2026, 10, d, 9, tzinfo=KST), 9000.0) for d in range(1, 31)]

    def run_from(self, now):
        return rank_history.forecast(self.flat, 9000, now, self.end, (200, 100, 300))['future']

    def test_adds_only_surge_left_after_now(self):
        now = datetime(2026, 11, 9, 21, tzinfo=KST)
        done = rank_history._surge_share(2.5)
        first, last = self.run_from(now)[0], self.run_from(now)[-1]
        self.assertAlmostEqual(first[1], 9000 + (rank_history._surge_share(2) - done) * 200, places=6)
        self.assertAlmostEqual(last[1], 9000 + (1 - done) * 200, places=6)

    def test_full_surge_when_far_from_end(self):
        future = self.run_from(datetime(2026, 10, 20, 9, tzinfo=KST))
        self.assertAlmostEqual(future[-1][1], 9200, places=6)


if __name__ == '__main__':
    unittest.main()
