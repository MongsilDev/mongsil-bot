import json
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

import helpers  # noqa: F401

import discord

from commands import alerts, settings
from utils import guild_settings
from utils.usage_db import db

GUILD = 1


def article(aid, title='새 글', created=None):
    created = created or datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    return {'id': aid, 'category_id': 27, 'thumbnail_url': f'https://example.com/{aid}.png', 'created_at': created,
            'i18ns': {'ko_KR': {'title': title}}, 'url': f'https://example.com/posts/{aid}'}


def make_alerts():
    cog = alerts.Alerts.__new__(alerts.Alerts)
    cog.client, cog._failures = None, {}
    cog.deliver = mock.AsyncMock(return_value=1)
    return cog


def clear():
    db.execute("DELETE FROM guild_settings")
    db.execute("DELETE FROM bot_state")
    db.execute("DELETE FROM command_log")
    guild_settings._cache.clear()


class MigrateTest(unittest.TestCase):
    def setUp(self):
        clear()

    def test_moves_maintenance_to_news(self):
        db.execute("INSERT INTO guild_settings VALUES (?, 'alert_maintenance', ?)", (GUILD, json.dumps(11)))
        db.execute("INSERT INTO guild_settings VALUES (?, 'alert_maintenance', ?)", (2, json.dumps(21)))
        db.execute("INSERT INTO guild_settings VALUES (?, 'alert_news', ?)", (2, json.dumps(22)))
        guild_settings.migrate()
        rows = {(r['guild_id'], r['key']): json.loads(r['value']) for r in db.execute("SELECT * FROM guild_settings")}
        self.assertEqual(rows, {(GUILD, 'alert_news'): 11, (2, 'alert_news'): 22})
        # 한 번 더 돌아도 끈 새소식 알림이 되살아나지 않음
        db.execute("DELETE FROM guild_settings WHERE guild_id = ?", (GUILD,))
        guild_settings.migrate()
        self.assertIsNone(db.execute("SELECT 1 FROM guild_settings WHERE guild_id = ?", (GUILD,)).fetchone())

    def test_rows(self):
        self.assertEqual([name for _, name, _ in guild_settings.ALERTS],
                         ['새소식', '패치노트', 'E스포츠', '이벤트', '공식 방송', '개발 일지', '매일 이터컷'])
        self.assertNotIn('alert_maintenance', guild_settings.DEFAULTS)


class CheckTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear()

    async def check(self, cog, key, path, name, articles):
        with mock.patch.object(alerts.api_client, 'get', mock.AsyncMock(return_value={'articles': articles})) as get:
            await cog.check_category(key, path, name)
        self.assertIn(f'category={path}&', get.call_args.args[0])

    async def test_first_check_only_records(self):
        cog = make_alerts()
        await self.check(cog, 'alert_event', 'event', '이벤트', [article(2), article(1)])
        cog.deliver.assert_not_called()
        self.assertEqual(guild_settings.state('seen_event'), [2, 1])

    async def test_sends_only_new(self):
        cog = make_alerts()
        await self.check(cog, 'alert_esports', 'esports', 'E스포츠', [article(1)])
        await self.check(cog, 'alert_esports', 'esports', 'E스포츠', [article(3), article(2, ''), article(1)])
        self.assertEqual(cog.deliver.await_count, 1)
        key, make = cog.deliver.await_args.args
        self.assertEqual(key, 'alert_esports')
        view = make()
        self.assertEqual(helpers.text_of(view), '### 새 글')
        button = next(i for i in view.walk_children() if isinstance(i, discord.ui.Button))
        self.assertEqual((button.label, button.url), ('E스포츠', 'https://example.com/posts/3'))
        self.assertEqual(guild_settings.state('seen_esports'), [3, 2, 1])
        # 빈 제목 글은 기록만 하고 다시 보지 않음
        await self.check(cog, 'alert_esports', 'esports', 'E스포츠', [article(3), article(2, ''), article(1)])
        self.assertEqual(cog.deliver.await_count, 1)

    async def test_patch_keeps_old_state(self):
        guild_settings.set_state('seen_patches', [1])
        cog = make_alerts()
        await self.check(cog, 'alert_patch', 'patchnote', '패치노트', [article(2), article(1)])
        self.assertEqual(cog.deliver.await_count, 1)
        self.assertEqual(guild_settings.state('seen_patches'), [2, 1])

    async def test_old_article_not_sent(self):
        guild_settings.set_state('seen_news', [1])
        cog = make_alerts()
        await self.check(cog, 'alert_news', 'news', '새소식', [article(2, created='2020-01-01T00:00:00Z'), article(1)])
        cog.deliver.assert_not_called()

    async def test_watch_news_checks_all(self):
        cog = make_alerts()
        cog.check_category = mock.AsyncMock(side_effect=[None, RuntimeError('형식 바뀜'), None, None, None, None])
        await cog.watch_news.coro(cog)
        self.assertEqual([c.args[1] for c in cog.check_category.await_args_list],
                         ['news', 'patchnote', 'esports', 'event', 'broadcasts', 'dev'])
        self.assertEqual(cog._failures['패치노트'], 1)
        self.assertEqual(cog._failures['새소식'], 0)


class SettingsViewTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clear()

    async def test_component_count(self):
        for n, (key, _, _) in enumerate(guild_settings.ALERTS):
            guild_settings.put(GUILD, key, 100 + n)
        db.execute("INSERT INTO command_log (ts, guild_id, user_id, command, status) VALUES (?, ?, 1, '랭크', 'ok')",
                   (int(time.time()), GUILD))
        with mock.patch.object(settings, 'load_disabled_servers', return_value=set()):
            view = settings.SettingsView(GUILD, 100)
        self.assertLessEqual(view.total_children_count, 40)
        texts = helpers.text_of(view)
        self.assertIn('**새소식** <#100>', texts)
        self.assertNotIn('**새소식** <#100>\n-#', texts)
        self.assertIn('-# 아시아1 이터니티와 데미갓 컷', texts)


if __name__ == '__main__':
    unittest.main()
