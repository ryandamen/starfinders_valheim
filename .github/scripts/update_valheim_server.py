"""Run GTXGaming's configured mod-update script through the TCAdmin API."""

import argparse
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

REQUEST_TIMEOUT_SECONDS = 60
SCRIPT_TIMEOUT_SECONDS = 30 * 60
STATUS_POLL_SECONDS = 10
STATUS_TIMEOUT_SECONDS = 300
NOTICE_DELAYS_SECONDS = (5 * 60, 5 * 60, 2 * 60, 60, 60, 60)
NOTICE_MINUTES = (15, 10, 5, 3, 2, 1, 0)
AWL_WEBHOOK_USERNAME = "OnlyMods"
SAVE_SETTLE_SECONDS = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def validate_webhook_url(webhook_url):
    parsed_url = urllib.parse.urlsplit(webhook_url)
    if parsed_url.scheme != "https" or parsed_url.hostname not in {
        "discord.com",
        "discordapp.com",
    }:
        raise ValueError(
            "DISCORDBOT_AWL_WEBHOOK_URL must be an HTTPS Discord webhook"
        )


def webhook_message(webhook_url, content):
    validate_webhook_url(webhook_url)
    payload = json.dumps(
        {
            "content": content,
            "username": AWL_WEBHOOK_USERNAME,
            "allowed_mentions": {"parse": []},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(
        request, timeout=REQUEST_TIMEOUT_SECONDS
    ) as response:
        if response.status not in (200, 204):
            raise RuntimeError(
                f"Discord webhook returned unexpected status {response.status}"
            )


def send_game_message(webhook_url, message):
    webhook_message(webhook_url, f"!message {message}")


def send_countdown(webhook_url, package_version):
    for index, minutes in enumerate(NOTICE_MINUTES):
        if index:
            time.sleep(NOTICE_DELAYS_SECONDS[index - 1])

        if minutes == 0:
            send_game_message(
                webhook_url,
                f"The server is restarting now to install modpack "
                f"{package_version}.",
            )
            webhook_message(webhook_url, "!save")
            time.sleep(SAVE_SETTLE_SECONDS)
            return

        unit = "minute" if minutes == 1 else "minutes"
        send_game_message(
            webhook_url,
            f"The server will restart in {minutes} {unit} to install modpack "
            f"{package_version}.",
        )

    raise RuntimeError("Restart countdown ended without reaching zero")


class TcAdminClient:
    def __init__(self, panel_url, api_key, service_id):
        parsed_url = urllib.parse.urlsplit(panel_url)
        if (
            parsed_url.scheme != "https"
            or not parsed_url.netloc
            or parsed_url.username
            or parsed_url.password
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("TCADMIN_PANEL_URL must be an HTTPS base URL")
        if not api_key:
            raise ValueError("TCADMIN_API_KEY must not be empty")
        if not service_id.isdigit():
            raise ValueError("TCADMIN_SERVICE_ID must be numeric")

        self.panel_url = panel_url.rstrip("/")
        self.api_key = api_key
        self.service_id = service_id
        self.service_path = (
            f"/api/service/{urllib.parse.quote(service_id, safe='')}"
        )

    def _request(
        self,
        method,
        path,
        fields=None,
        timeout=REQUEST_TIMEOUT_SECONDS,
    ):
        headers = {
            "Accept": "application/json",
            "api_key": self.api_key,
        }
        data = None
        if method == "POST":
            boundary = "----Starfinders" + uuid.uuid4().hex
            body = []
            for key, value in (fields or {}).items():
                body.append(
                    (
                        f"--{boundary}\r\n"
                        f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                        f"{value}\r\n"
                    ).encode("utf-8")
                )
            body.append(f"--{boundary}--\r\n".encode("ascii"))
            data = b"".join(body)
            headers["Content-Type"] = (
                f"multipart/form-data; boundary={boundary}"
            )

        request = urllib.request.Request(
            self.panel_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=timeout
            ) as response:
                payload = response.read()
                status_code = response.status
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                f"TCAdmin {method} request failed with HTTP "
                f"{error.code}: {detail}"
            ) from error

        return self._validate_response(payload, method, status_code)

    @staticmethod
    def _validate_response(payload, method, status_code):
        if not 200 <= status_code < 300:
            detail = payload.decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                f"TCAdmin {method} request failed with HTTP "
                f"{status_code}: {detail}"
            )
        try:
            result = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"TCAdmin {method} request returned invalid JSON"
            ) from error
        if not isinstance(result, dict) or result.get("Success") is not True:
            message = (
                result.get("Message", "unknown API error")
                if isinstance(result, dict)
                else "unexpected API response"
            )
            raise RuntimeError(f"TCAdmin {method} request failed: {message}")
        return result.get("Result")

    def service_status(self):
        result = self._request("GET", self.service_path)
        if not isinstance(result, dict) or not isinstance(
            result.get("ServiceStatus"), int
        ):
            raise RuntimeError("TCAdmin service response has no ServiceStatus")
        return result["ServiceStatus"]

    def wait_for_status(self, desired_status):
        deadline = time.monotonic() + STATUS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.service_status() == desired_status:
                return
            time.sleep(STATUS_POLL_SECONDS)
        raise TimeoutError(
            f"TCAdmin service did not reach status {desired_status} "
            f"within {STATUS_TIMEOUT_SECONDS} seconds"
        )

    def stop_service(self):
        self._request(
            "POST",
            f"{self.service_path}/stop",
            fields={"reason": "Starfinders modpack update"},
        )

    def run_mod_update_script(self, script_id, mod_id):
        if not script_id.isdigit() or not mod_id.isdigit():
            raise ValueError("TCADMIN script and mod IDs must be numeric")

        result = self._request(
            "POST",
            f"{self.service_path}/Script/"
            f"{urllib.parse.quote(script_id, safe='')}",
            fields={
                "variables": "{}",
                "runNow": "true",
                "modId": mod_id,
                "isGlobal": "false",
            },
            timeout=SCRIPT_TIMEOUT_SECONDS,
        )
        if not isinstance(result, dict) or result.get("RunNow") is not True:
            raise RuntimeError(
                "TCAdmin did not confirm immediate execution of the mod "
                "update script"
            )

    def start_service(self):
        self._request(
            "POST",
            f"{self.service_path}/start",
            fields={"reason": "Starfinders modpack update"},
        )


def report_failure(webhook_url, package_version, shutdown_attempted):
    if shutdown_attempted:
        logger.error(
            "Automatic mod update for modpack %s failed after a server stop "
            "was requested. The service may remain stopped; check TCAdmin "
            "and the configured update script.",
            package_version,
        )
        return

    send_game_message(
        webhook_url,
        f"Automatic mod update for modpack {package_version} failed before "
        "the server was stopped. The server was left running.",
    )


def update_server(args):
    webhook_url = args.discordbot_awl_webhook_url
    validate_webhook_url(webhook_url)
    client = TcAdminClient(
        args.tcadmin_panel_url,
        args.tcadmin_api_key,
        args.tcadmin_service_id,
    )
    shutdown_attempted = False
    try:
        status = client.service_status()
        if status != 4:
            raise RuntimeError(
                "Refusing to update because the TCAdmin service is not "
                f"running (ServiceStatus={status}, expected 4)"
            )

        send_countdown(webhook_url, args.version)
        logger.info("Stopping TCAdmin service %s.", args.tcadmin_service_id)
        shutdown_attempted = True
        client.stop_service()
        client.wait_for_status(1)

        logger.info(
            "Running TCAdmin mod-update script %s for mod %s.",
            args.tcadmin_script_id,
            args.tcadmin_mod_id,
        )
        client.run_mod_update_script(
            args.tcadmin_script_id,
            args.tcadmin_mod_id,
        )

        logger.info("Starting TCAdmin service %s.", args.tcadmin_service_id)
        client.start_service()
        client.wait_for_status(4)
        send_game_message(
            webhook_url,
            f"The server is back online with modpack {args.version}.",
        )
    except (
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
        urllib.error.URLError,
    ):
        report_failure(webhook_url, args.version, shutdown_attempted)
        raise


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run the GTXGaming mod updater after a Thunderstore release."
    )
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--tcadmin-panel-url",
        default=os.environ.get("TCADMIN_PANEL_URL"),
        required=os.environ.get("TCADMIN_PANEL_URL") is None,
    )
    parser.add_argument(
        "--tcadmin-api-key",
        default=os.environ.get("TCADMIN_API_KEY"),
        required=os.environ.get("TCADMIN_API_KEY") is None,
    )
    parser.add_argument(
        "--tcadmin-service-id",
        default=os.environ.get("TCADMIN_SERVICE_ID"),
        required=os.environ.get("TCADMIN_SERVICE_ID") is None,
    )
    parser.add_argument(
        "--tcadmin-script-id",
        default=os.environ.get("TCADMIN_SCRIPT_ID"),
        required=os.environ.get("TCADMIN_SCRIPT_ID") is None,
    )
    parser.add_argument(
        "--tcadmin-mod-id",
        default=os.environ.get("TCADMIN_MOD_ID"),
        required=os.environ.get("TCADMIN_MOD_ID") is None,
    )
    parser.add_argument(
        "--discordbot-awl-webhook-url",
        default=os.environ.get("DISCORDBOT_AWL_WEBHOOK_URL"),
        required=os.environ.get("DISCORDBOT_AWL_WEBHOOK_URL") is None,
    )
    return parser.parse_args()


def main():
    update_server(parse_args())


if __name__ == "__main__":
    main()
