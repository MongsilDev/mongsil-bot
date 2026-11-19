import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import helpers  # noqa: F401

from commands import rating
from utils import labs_cut

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/cut.json').read_text())


class LabsCutTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = TemporaryDirectory()
        self.cut = Path(self.dir.name, 'cut.json')
        self.top = Path(self.dir.name, 'top.json')
        self.race = Path(self.dir.name, 'race.json')
        patches = [mock.patch.object(labs_cut, 'CUT', str(self.cut)), mock.patch.object(labs_cut, 'TOP', str(self.top)),
                   mock.patch.object(labs_cut, 'RACE', str(self.race))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        labs_cut._cache.clear()

    def tearDown(self):
        self.dir.cleanup()

    def write(self, data, **extra):
        self.cut.write_text(json.dumps({**data, **extra}))
        labs_cut._cache.clear()

    async def test_other_season_is_ignored(self):
        self.write(FIXTURE)
        self.assertIsNone(labs_cut.cut(43))
        self.assertEqual(await rating.fetch_rating_info(None, 43), (None, None))

    async def test_boards_come_from_file(self):
        self.write(FIXTURE)
        a, b = await rating.fetch_rating_info(None, 41)
        self.assertEqual((a['mmr'], b['mmr']), (FIXTURE['tiers']['eternity']['board'], FIXTURE['tiers']['demigod']['board']))

    def test_tier_tuple_shape(self):
        data = json.loads(json.dumps(FIXTURE))
        data['tiers']['eternity'].update(cut=9100, dropped=12, estimated=True,
                                         forecast=[[1795000000, 9150.0, 9100.0, 9200.0, 9090.0]])
        data['tiers']['demigod'].update(dropped=None)
        eternity, demigod = rating.tier_cuts(data)
        self.assertEqual(eternity[5], (9100, 12, True))
        self.assertEqual(eternity[6], 9100)
        self.assertEqual(eternity[7]['future'][0][1:], (9150.0, 9100.0, 9200.0))
        self.assertIsNone(demigod[5])

    def test_card_marks_stale_data(self):
        self.write(FIXTURE, updatedAt=round(time.time()) - 4 * 3600)
        view = rating.create_rating_layout(labs_cut.cut(41), '정규 시즌 12', 41)
        text = '\n'.join(c.content for c in view.walk_children() if hasattr(c, 'content'))
        self.assertIn(':00 기준', text)
        self.assertIn('늦어지고', text)

    def test_top_needs_same_season(self):
        self.top.write_text(json.dumps({'at': 0, 'season': 41, 'topRanks': [{'nickname': 'a', 'rank': 1, 'mmr': 9000}]}))
        self.assertEqual(len(labs_cut.top(41)), 1)
        self.assertIsNone(labs_cut.top(43))


    def card_text(self):
        view = rating.create_rating_layout(labs_cut.cut(41), '정규 시즌 12', 41)
        return '\n'.join(c.content for c in view.walk_children() if hasattr(c, 'content'))

    def write_race(self, **extra):
        now = round(time.time())
        lst = [{'nickname': f'p_{i}', 'rp': 8400 - i, 'at': now - 3600 + i} for i in range(6)] + [{'nickname': 'late', 'rp': 8300, 'at': None}]
        self.race.write_text(json.dumps({'updatedAt': now, 'season': 41, 'open': True, 'count': len(lst), 'list': lst, **extra}))
        labs_cut._cache.clear()

    def test_card_shows_race_while_open(self):
        self.write(FIXTURE, updatedAt=round(time.time()))
        self.write_race()
        text = self.card_text()
        self.assertIn('이터니티 8,300 달성 **7**명', text)
        self.assertIn('`1` **p\\_0** 8,400 RP `', text)
        self.assertNotIn('p\\_5', text)
        self.assertIn('데미갓 1,000위', text)
        self.assertNotIn('이터니티 300위', text)

    def test_card_ignores_closed_or_stale_race(self):
        self.write(FIXTURE, updatedAt=round(time.time()))
        for extra in ({'open': False}, {'season': 43}, {'updatedAt': round(time.time()) - 4 * 3600}):
            self.write_race(**extra)
            self.assertIn('이터니티 300위', self.card_text())

if __name__ == '__main__':
    unittest.main()
