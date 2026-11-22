import unittest
from unittest import mock

import helpers  # noqa: F401

import discord

from utils import emoji_zoom


class WebhookForbiddenNotifyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        emoji_zoom._webhook_cache.clear()
        emoji_zoom._webhook_permission_notified.clear()

    async def test_forum_post_notifies_thread(self):
        guild = mock.Mock(id=1)
        guild.me.guild_permissions.manage_webhooks = False
        forum = mock.Mock(spec=discord.ForumChannel, id=10, guild=guild)
        forum.webhooks = mock.AsyncMock(return_value=[])
        forum.create_webhook = mock.AsyncMock(
            side_effect=discord.Forbidden(mock.Mock(status=403, reason="Forbidden"), "Missing Permissions"))
        thread = mock.Mock(spec=discord.Thread)
        thread.send = mock.AsyncMock()

        with mock.patch.object(emoji_zoom, "load_disabled_servers", return_value=set()), \
             mock.patch.object(emoji_zoom, "save_disabled_servers", return_value=True), \
             mock.patch.object(emoji_zoom.guild_settings, "log"):
            result = await emoji_zoom.get_or_create_webhook(forum, thread)

        self.assertIsNone(result)
        thread.send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
