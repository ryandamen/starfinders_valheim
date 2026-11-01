import os
import sys
import threading
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import update_valheim_server as updater
from thunderstore_deployment import StagedDeployment


class ArgumentParsingTests(unittest.TestCase):
    def test_check_connection_mode_is_not_available(self):
        with (
            mock.patch.object(Path, "is_file", return_value=False),
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                sys,
                "argv",
                [
                    "update_valheim_server.py",
                    "--version",
                    "1.2.3",
                    "--check-connection",
                ],
            ),
            redirect_stderr(StringIO()) as error_output,
        ):
            with self.assertRaises(SystemExit) as error:
                updater.parse_args()

        self.assertEqual(error.exception.code, 2)
        self.assertIn(
            "unrecognized arguments: --check-connection",
            error_output.getvalue(),
        )

    def test_invocation_requires_version(self):
        with (
            mock.patch.object(Path, "is_file", return_value=False),
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                sys,
                "argv",
                ["update_valheim_server.py"],
            ),
            redirect_stderr(StringIO()) as error_output,
        ):
            with self.assertRaises(SystemExit) as error:
                updater.parse_args()

        self.assertEqual(error.exception.code, 2)
        self.assertIn("--version is required", error_output.getvalue())

    def test_dry_run_is_not_supported(self):
        with (
            mock.patch.object(Path, "is_file", return_value=False),
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                sys,
                "argv",
                [
                    "update_valheim_server.py",
                    "--version",
                    "1.2.3",
                    "--dry-run",
                ],
            ),
            redirect_stderr(StringIO()) as error_output,
        ):
            with self.assertRaises(SystemExit) as error:
                updater.parse_args()

        self.assertEqual(error.exception.code, 2)
        self.assertIn(
            "unrecognized arguments: --dry-run",
            error_output.getvalue(),
        )

    def test_configured_invocation_needs_no_execution_flag(self):
        config = {
            "TCADMIN_API_KEY": "key",
            "TCADMIN_SERVICE_ID": "123",
            "DISCORDBOT_AWL_WEBHOOK_URL": "https://discord.com/api/webhooks/1/token",
            "SFTP_HOST": "sftp.example.com",
            "SFTP_USERNAME": "user",
            "SFTP_PASSWORD": "password",
            "SFTP_KNOWN_HOSTS": "[sftp.example.com]:8822 ssh-ed25519 key",
            "SFTP_REMOTE_PATH": "/server/BepInEx/plugins",
            "SFTP_PORT": "8822",
        }
        with (
            mock.patch.object(Path, "is_file", return_value=False),
            mock.patch.dict(os.environ, config, clear=True),
            mock.patch.object(
                sys,
                "argv",
                ["update_valheim_server.py", "--version", "1.2.3"],
            ),
        ):
            args = updater.parse_args()

        self.assertEqual(args.version, "1.2.3")
        self.assertEqual(args.sftp_port, 8822)


class TcAdminTests(unittest.TestCase):
    def test_requires_https_and_numeric_service_id(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            updater.TcAdminClient("http://panel.example", "key", "123")
        with self.assertRaisesRegex(ValueError, "numeric"):
            updater.TcAdminClient(
                "https://panel.example",
                "key",
                "not-a-number",
            )

    def test_service_status_uses_configured_api_key_header(self):
        response = mock.Mock()
        response.json.return_value = {
            "Success": True,
            "Result": {"ServiceStatus": 4},
        }
        response.raise_for_status.return_value = None
        client = updater.TcAdminClient(
            "https://panel.example",
            "secret",
            "123",
            api_key_header="X-Api-Key",
        )
        with mock.patch.object(
            updater.requests,
            "get",
            return_value=response,
        ) as get:
            self.assertEqual(client.service_status(), 4)

        self.assertEqual(
            get.call_args.kwargs["headers"]["X-Api-Key"],
            "secret",
        )
        self.assertEqual(
            get.call_args.args[0],
            "https://panel.example/api/service/123",
        )

    def test_restart_uses_tcadmin_service_endpoint(self):
        client = updater.TcAdminClient(
            "https://panel.example",
            "secret",
            "123",
        )
        client._request = mock.Mock()

        client.restart_service()

        self.assertEqual(
            client._request.call_args_list,
            [
                mock.call(
                    "POST",
                    "/api/service/123/restart",
                    fields={"reason": "Starfinders modpack update"},
                ),
            ],
        )

    def test_wait_for_status_polls_every_second(self):
        client = updater.TcAdminClient(
            "https://panel.example",
            "secret",
            "123",
        )
        client.service_status = mock.Mock(side_effect=[2, 4])
        with (
            mock.patch.object(
                updater.time,
                "monotonic",
                side_effect=[0, 0, 1],
            ),
            mock.patch.object(updater.time, "sleep") as sleep,
        ):
            client.wait_for_status(4)

        sleep.assert_called_once_with(1)

    def test_failed_api_response_is_reported(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "Success": False,
            "Message": "denied",
        }
        client = updater.TcAdminClient(
            "https://panel.example",
            "secret",
            "123",
        )
        with (
            mock.patch.object(
                updater.requests,
                "get",
                return_value=response,
            ),
            self.assertRaisesRegex(RuntimeError, "denied"),
        ):
            client.service_status()


class CountdownTests(unittest.TestCase):
    def test_uses_requested_restart_warning_minutes(self):
        messages = []
        with (
            mock.patch.object(
                updater,
                "send_game_message",
                side_effect=lambda webhook, message: messages.append(message),
            ),
            mock.patch.object(
                updater,
                "_wait_for_countdown_delay",
                return_value=True,
            ) as wait_for_delay,
        ):
            result = updater.send_countdown(
                "https://discord.com/api/webhooks/example/token",
                "1.2.3",
                threading.Event(),
            )

        self.assertTrue(result)
        self.assertEqual(
            [
                "Server restarting in 10 minutes.",
                "Server restarting in 5 minutes.",
                "Server restarting in 3 minutes.",
                "Server restarting in 2 minutes.",
                "Server restarting in 1 minute.",
            ],
            messages,
        )
        self.assertEqual(wait_for_delay.call_count, 5)

    def test_package_preparation_and_countdown_run_concurrently(self):
        args = SimpleNamespace(
            version="1.2.3",
            discordbot_awl_webhook_url="https://discord.com/webhook",
        )
        barrier = threading.Barrier(2)
        staged = StagedDeployment("/stage", ("Example/plugin.dll",))

        def prepare(_args, _cancel_event):
            barrier.wait(timeout=2)
            return staged

        def countdown(_webhook, _version, _cancel_event):
            barrier.wait(timeout=2)
            return True

        with (
            mock.patch.object(updater, "_stage_packages", side_effect=prepare),
            mock.patch.object(updater, "send_countdown", side_effect=countdown),
        ):
            result = updater._prepare_update(args, threading.Event())

        self.assertEqual(result, staged)


class UpdateSequenceTests(unittest.TestCase):
    def setUp(self):
        self.args = SimpleNamespace(
            version="1.2.3",
            discordbot_awl_webhook_url=(
                "https://discord.com/api/webhooks/example/token"
            ),
            tcadmin_panel_url="https://panel.example",
            tcadmin_api_key="secret",
            tcadmin_api_key_header="api_key",
            tcadmin_service_id="5635104",
            sftp_host="server.example",
            sftp_port=8822,
            sftp_username="user",
            sftp_password="password",
            sftp_known_hosts="[server.example]:8822 ssh-ed25519 key",
            sftp_remote_path="/server/BepInEx/plugins",
        )

    def test_server_restarts_after_live_promotion_and_save(self):
        calls = []
        staged = StagedDeployment("/stage", ("Example/plugin.dll",))

        class FakeClient:
            def __init__(self, *args):
                calls.append("client")

            def service_status(self):
                calls.append("status")
                return 4

            def wait_for_status(self, status):
                calls.append(f"wait-{status}")

            def restart_service(self):
                calls.append("restart")

        with (
            mock.patch.object(updater, "TcAdminClient", FakeClient),
            mock.patch.object(
                updater,
                "_prepare_update",
                side_effect=lambda *_: calls.append("prepare") or staged,
            ),
            mock.patch.object(
                updater,
                "announce_restart_and_save",
                side_effect=lambda *_: calls.append("save"),
            ),
            mock.patch.object(
                updater,
                "promote_staged_deployment",
                side_effect=lambda *_: calls.append("promote"),
            ),
            mock.patch.object(
                updater,
                "send_game_message",
                side_effect=lambda *_: calls.append("online"),
            ),
        ):
            updater.update_server(self.args)

        self.assertEqual(
            calls,
            [
                "client",
                "status",
                "prepare",
                "promote",
                "save",
                "restart",
                "wait-1",
                "wait-4",
                "online",
            ],
        )

    def test_preparation_failure_leaves_server_running(self):
        calls = []

        class FakeClient:
            def __init__(self, *args):
                pass

            def service_status(self):
                return 4

        with (
            mock.patch.object(updater, "TcAdminClient", FakeClient),
            mock.patch.object(
                updater,
                "_prepare_update",
                side_effect=RuntimeError("download failed"),
            ),
            mock.patch.object(
                updater,
                "send_game_message",
                side_effect=lambda *_: calls.append("failure-notice"),
            ),
            self.assertRaisesRegex(RuntimeError, "download failed"),
        ):
            updater.update_server(self.args)

        self.assertEqual(calls, ["failure-notice"])

    def test_promotion_failure_does_not_restart_server(self):
        calls = []
        staged = StagedDeployment("/stage", ("Example/plugin.dll",))

        class FakeClient:
            def __init__(self, *args):
                pass

            def service_status(self):
                return 4

            def restart_service(self):
                calls.append("restart")

        with (
            mock.patch.object(updater, "TcAdminClient", FakeClient),
            mock.patch.object(updater, "_prepare_update", return_value=staged),
            mock.patch.object(updater, "announce_restart_and_save"),
            mock.patch.object(
                updater,
                "promote_staged_deployment",
                side_effect=RuntimeError("promotion failed"),
            ),
            mock.patch.object(
                updater,
                "send_game_message",
                side_effect=lambda *_: calls.append("failure-notice"),
            ),
            self.assertRaisesRegex(RuntimeError, "promotion failed"),
        ):
            updater.update_server(self.args)

        self.assertEqual(calls, ["failure-notice"])


if __name__ == "__main__":
    unittest.main()
