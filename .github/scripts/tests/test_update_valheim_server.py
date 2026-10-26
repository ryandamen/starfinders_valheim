import json
import sys
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import update_valheim_server as updater


class TcAdminTests(unittest.TestCase):
    def test_requires_https_numeric_service_and_script_ids(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            updater.TcAdminClient("http://panel.example", "key", "123")
        with self.assertRaisesRegex(ValueError, "numeric"):
            updater.TcAdminClient(
                "https://panel.example",
                "key",
                "not-a-number",
            )

        client = updater.TcAdminClient(
            "https://panel.example",
            "secret-key",
            "123",
        )
        with self.assertRaisesRegex(ValueError, "numeric"):
            client.run_mod_update_script("script", "4")

    def test_mod_update_script_uses_tcadmin_custom_script_api(self):
        client = updater.TcAdminClient(
            "https://panel.example",
            "secret-key",
            "5635104",
        )
        client._request = mock.Mock(return_value={"RunNow": True})

        client.run_mod_update_script("72", "4")

        client._request.assert_called_once_with(
            "POST",
            "/api/service/5635104/Script/72",
            fields={
                "variables": "{}",
                "runNow": "true",
                "modId": "4",
                "isGlobal": "false",
            },
            timeout=updater.SCRIPT_TIMEOUT_SECONDS,
        )

    def test_mod_update_script_fails_if_not_confirmed_immediate(self):
        client = updater.TcAdminClient(
            "https://panel.example",
            "secret-key",
            "5635104",
        )
        client._request = mock.Mock(return_value={"RunNow": False})

        with self.assertRaisesRegex(RuntimeError, "immediate execution"):
            client.run_mod_update_script("72", "4")

    def test_tcadmin_api_uses_api_key_and_validates_response(self):
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"Success": true, "Result": {"RunNow": true}}'

        client = updater.TcAdminClient(
            "https://panel.example",
            "secret-key",
            "5635104",
        )
        with mock.patch.object(
            updater.urllib.request,
            "urlopen",
            return_value=FakeResponse(),
        ) as urlopen:
            result = client._request(
                "POST",
                "/api/service/5635104/Script/72",
                fields={
                    "variables": "{}",
                    "runNow": "true",
                    "modId": "4",
                    "isGlobal": "false",
                },
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Api_key"), "secret-key")
        self.assertIn(b'name="runNow"\r\n\r\ntrue', request.data)
        self.assertEqual(result, {"RunNow": True})

    def test_awl_webhook_uses_configured_identity_and_disables_mentions(self):
        class FakeResponse:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with mock.patch.object(
            updater.urllib.request,
            "urlopen",
            return_value=FakeResponse(),
        ) as urlopen:
            updater.webhook_message(
                "https://discord.com/api/webhooks/123/key",
                "!message Maintenance notice",
            )

        request = urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload["username"], updater.AWL_WEBHOOK_USERNAME)
        self.assertEqual(payload["content"], "!message Maintenance notice")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})


class UpdateSequenceTests(unittest.TestCase):
    def setUp(self):
        self.args = SimpleNamespace(
            version="3.2.3",
            tcadmin_panel_url="https://panel.example",
            tcadmin_api_key="secret",
            tcadmin_service_id="5635104",
            tcadmin_script_id="72",
            tcadmin_mod_id="4",
            discordbot_awl_webhook_url="https://discord.com/api/webhooks/123/key",
        )

    def test_update_stops_runs_mod_updater_and_starts_in_order(self):
        calls = []

        class FakeClient:
            def __init__(self, *args):
                calls.append("client")

            def service_status(self):
                calls.append("status")
                return 4

            def stop_service(self):
                calls.append("stop")

            def wait_for_status(self, status):
                calls.append(f"wait-{status}")

            def run_mod_update_script(self, script_id, mod_id):
                calls.append(f"update-{script_id}-{mod_id}")

            def start_service(self):
                calls.append("start")

        with (
            mock.patch.object(updater, "TcAdminClient", FakeClient),
            mock.patch.object(
                updater,
                "send_countdown",
                side_effect=lambda *args: calls.append("countdown"),
            ),
            mock.patch.object(
                updater,
                "send_game_message",
                side_effect=lambda *args: calls.append("online-message"),
            ),
        ):
            updater.update_server(self.args)

        self.assertEqual(
            calls,
            [
                "client",
                "status",
                "countdown",
                "stop",
                "wait-1",
                "update-72-4",
                "start",
                "wait-4",
                "online-message",
            ],
        )

    def test_failed_mod_update_does_not_restart_service(self):
        calls = []

        class FakeClient:
            def __init__(self, *args):
                pass

            def service_status(self):
                return 4

            def stop_service(self):
                calls.append("stop")

            def wait_for_status(self, status):
                calls.append(f"wait-{status}")

            def run_mod_update_script(self, script_id, mod_id):
                calls.append("update")
                raise RuntimeError("mod update failed")

            def start_service(self):
                calls.append("start")

        with (
            mock.patch.object(updater, "TcAdminClient", FakeClient),
            mock.patch.object(updater, "send_countdown"),
        ):
            with self.assertLogs(updater.logger, level="ERROR") as captured:
                with self.assertRaisesRegex(RuntimeError, "mod update failed"):
                    updater.update_server(self.args)

        self.assertEqual(calls, ["stop", "wait-1", "update"])
        self.assertIn("may remain stopped", captured.output[-1])

    def test_countdown_uses_requested_offsets_and_save_delay(self):
        with (
            mock.patch.object(updater.time, "sleep") as sleep,
            mock.patch.object(updater, "webhook_message") as command,
        ):
            updater.send_countdown(
                self.args.discordbot_awl_webhook_url,
                self.args.version,
            )

        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [*updater.NOTICE_DELAYS_SECONDS, updater.SAVE_SETTLE_SECONDS],
        )
        self.assertEqual(command.call_count, 8)
        self.assertEqual(
            [call.args[1] for call in command.call_args_list],
            [
                "!message The server will restart in 15 minutes to install modpack 3.2.3.",
                "!message The server will restart in 10 minutes to install modpack 3.2.3.",
                "!message The server will restart in 5 minutes to install modpack 3.2.3.",
                "!message The server will restart in 3 minutes to install modpack 3.2.3.",
                "!message The server will restart in 2 minutes to install modpack 3.2.3.",
                "!message The server will restart in 1 minute to install modpack 3.2.3.",
                "!message The server is restarting now to install modpack 3.2.3.",
                "!save",
            ],
        )


if __name__ == "__main__":
    unittest.main()
