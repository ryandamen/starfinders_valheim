import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live_test import check_discord_bot_config as tester


class DiscordBotConfigTests(unittest.TestCase):
    def test_reads_webhook_from_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "DISCORDBOT_AWL_WEBHOOK_URL="
                '"https://discord.com/api/webhooks/1/token"\n',
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {}, clear=True):
                webhook_url = tester.load_webhook_url(env_file)

        self.assertEqual(
            webhook_url,
            "https://discord.com/api/webhooks/1/token",
        )

    def test_environment_overrides_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "DISCORDBOT_AWL_WEBHOOK_URL="
                '"https://discord.com/api/webhooks/file/token"\n',
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "DISCORDBOT_AWL_WEBHOOK_URL":
                        "https://discord.com/api/webhooks/env/token"
                },
                clear=True,
            ):
                webhook_url = tester.load_webhook_url(env_file)

        self.assertEqual(
            webhook_url,
            "https://discord.com/api/webhooks/env/token",
        )

    def test_sends_awl_game_message_without_confirmation(self):
        args = mock.Mock(
            env_file=Path(".env"),
            message="DiscordBot_AWL config test.",
        )
        webhook_url = "https://discord.com/api/webhooks/1/token"
        with (
            mock.patch.object(tester, "parse_args", return_value=args),
            mock.patch.object(
                tester,
                "load_webhook_url",
                return_value=webhook_url,
            ),
            mock.patch.object(tester, "send_game_message") as send_message,
        ):
            self.assertEqual(tester.main(), 0)

        send_message.assert_called_once_with(
            webhook_url,
            "DiscordBot_AWL config test.",
        )


if __name__ == "__main__":
    unittest.main()
