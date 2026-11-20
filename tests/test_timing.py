import asyncio
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from helpers import db, reset  # noqa: F401

import discord

from utils import layouts, loop_watch
from utils.api_client import api_timing
from utils.usage_db import record_command


def interaction(age_ms=200):
    response = SimpleNamespace(
        is_done=lambda: False,
        send_message=mock.AsyncMock(return_value=SimpleNamespace(resource=SimpleNamespace())),
    )
    return SimpleNamespace(
        created_at=discord.utils.utcnow() - timedelta(milliseconds=age_ms),
        extras={}, response=response,
        edit_original_response=mock.AsyncMock(return_value=SimpleNamespace()),
        original_response=mock.AsyncMock(return_value=SimpleNamespace()),
    )


class SendCardTimingTest(unittest.IsolatedAsyncioTestCase):
    async def test_fast_reply_marks_built_before_shown(self):
        i = interaction()

        async def build():
            return discord.ui.LayoutView()
        await layouts.send_card(i, '불러오는 중', build)
        self.assertGreaterEqual(i.extras['built_ms'], 200)
        self.assertGreaterEqual(i.extras['shown_ms'], i.extras['built_ms'])

    async def test_slow_reply_marks_built_after_loading(self):
        i = interaction()

        async def build():
            await asyncio.sleep(0.05)
            return discord.ui.LayoutView()
        with mock.patch.object(layouts, 'FAST_REPLY_SECONDS', 0.01):
            await layouts.send_card(i, '불러오는 중', build)
        i.edit_original_response.assert_awaited_once()
        self.assertGreaterEqual(i.extras['built_ms'], 250)
        self.assertGreaterEqual(i.extras['shown_ms'], i.extras['built_ms'])

    async def test_zero_fast_reply_sends_loading_first(self):
        i = interaction()

        async def build():
            return discord.ui.LayoutView()
        await layouts.send_card(i, '불러오는 중', build, fast_reply=0)
        i.response.send_message.assert_awaited_once()
        self.assertNotIn('files', i.response.send_message.await_args.kwargs)
        i.edit_original_response.assert_awaited_once()
        self.assertGreaterEqual(i.extras['loading_ms'], i.extras['enter_ms'])
        self.assertGreaterEqual(i.extras['shown_ms'], i.extras['built_ms'])

    async def test_api_time_collected_only_inside_build(self):
        i = interaction()

        async def build():
            t = api_timing.get()
            t['api'] += 0.12
            t['sem'] += 0.03
            return discord.ui.LayoutView()
        await layouts.send_card(i, '불러오는 중', build)
        self.assertEqual((i.extras['api_ms'], i.extras['sem_ms']), (120, 30))
        self.assertIsNone(api_timing.get())


class RecordTest(unittest.TestCase):
    def setUp(self):
        reset()

    def test_columns_saved(self):
        record_command(1, 2, '랭크', 'ok', 1700, 1650, 180, 400)
        row = db.execute("SELECT ms, shown_ms, recv_ms, built_ms FROM command_log").fetchone()
        self.assertEqual(tuple(row), (1700, 1650, 180, 400))

    def test_build_breakdown_saved(self):
        record_command(1, 2, '랭크', 'ok', 1700, enter_ms=150, loading_ms=700, api_ms=300, sem_ms=0)
        row = db.execute("SELECT enter_ms, loading_ms, api_ms, sem_ms FROM command_log").fetchone()
        self.assertEqual(tuple(row), (150, 700, 300, 0))

    def test_old_call_leaves_new_columns_empty(self):
        record_command(1, 2, '설정', 'ok', 500)
        row = db.execute("SELECT recv_ms, built_ms FROM command_log").fetchone()
        self.assertEqual(tuple(row), (None, None))


def blocking_handler():
    time.sleep(1.4)


class LoopWatchTest(unittest.IsolatedAsyncioTestCase):
    async def test_stall_logs_running_code_once(self):
        loop_watch._thread = None
        with self.assertLogs('mongsil-bot.loop_watch', 'WARNING') as logs:
            loop_watch.start()
            await asyncio.sleep(0.3)
            blocking_handler()
            await asyncio.sleep(0.6)
        loop_watch._task.cancel()
        stacks = [m for m in logs.output if '막힘' in m]
        self.assertEqual(len(stacks), 1)
        self.assertIn('blocking_handler', stacks[0])
        self.assertTrue(any('지연' in m for m in logs.output))
