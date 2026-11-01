"""Deploy Thunderstore plugin files, then safely restart the Valheim server."""

import argparse
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.parse

import paramiko
import requests

from thunderstore_deployment import (
    deploy_to_sftp,
    discard_staged_deployment,
    promote_staged_deployment,
)

REQUEST_TIMEOUT_SECONDS = 60
STATUS_POLL_SECONDS = 1
STATUS_TIMEOUT_SECONDS = 300
NOTICE_DELAYS_SECONDS = (5 * 60, 2 * 60, 60, 60, 60)
NOTICE_MINUTES = (10, 5, 3, 2, 1)
AWL_WEBHOOK_USERNAME = "OnlyMods"
SAVE_SETTLE_SECONDS = 15

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)
OPERATION_ERRORS = (
    OSError,
    EOFError,
    RuntimeError,
    TimeoutError,
    ValueError,
    requests.RequestException,
    paramiko.SFTPError,
    paramiko.SSHException,
    urllib.error.URLError,
)


def positive_integer(value):
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a whole number") from error
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def read_env_file(path):
    values = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError(f"Could not read env file {path}: {error}") from error

    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        key, separator, raw_value = stripped.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(
                f"Invalid .env entry at {path}:{line_number}; "
                "expected KEY=VALUE"
            )

        raw_value = raw_value.strip()
        if raw_value.startswith(("'", '"')):
            quote = raw_value[0]
            closing_quote = raw_value.find(quote, 1)
            if closing_quote == -1:
                raise ValueError(
                    f"Invalid quoted value at {path}:{line_number}"
                )
            remainder = raw_value[closing_quote + 1 :].strip()
            if remainder and not remainder.startswith("#"):
                raise ValueError(
                    f"Invalid quoted value at {path}:{line_number}"
                )
            value = raw_value[1:closing_quote]
        else:
            value = raw_value.split("#", maxsplit=1)[0].strip()
        values[key] = value
    return values


def _environment_value(env_file_values, *keys):
    for key in keys:
        if key in os.environ:
            return os.environ[key]
        if key in env_file_values:
            return env_file_values[key]
    return None


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Download Thunderstore modpack dependencies, stage them over "
            "SFTP, and restart the Valheim server."
        )
    )
    parser.add_argument("--version")
    parser.add_argument(
        "--env-file",
        help="Local KEY=VALUE file (automatically reads .env when present).",
    )
    preliminary_args, _ = parser.parse_known_args()
    env_file_path = preliminary_args.env_file
    if env_file_path is None and Path(".env").is_file():
        env_file_path = ".env"
    try:
        env_file_values = (
            read_env_file(env_file_path) if env_file_path is not None else {}
        )
    except ValueError as error:
        parser.error(str(error))

    parser.set_defaults(
        tcadmin_panel_url=(
            _environment_value(
                env_file_values,
                "GTX_API_BASE_URL",
                "TCADMIN_PANEL_URL",
            )
            or "https://gamepanel.gtxgaming.co.uk"
        ),
        tcadmin_api_key=_environment_value(
            env_file_values,
            "GTX_API_KEY",
            "TCADMIN_API_KEY",
        ),
        tcadmin_api_key_header=(
            _environment_value(env_file_values, "GTX_API_KEY_HEADER")
            or "api_key"
        ),
        tcadmin_service_id=_environment_value(
            env_file_values,
            "TCADMIN_SERVICE_ID",
        ),
        discordbot_awl_webhook_url=_environment_value(
            env_file_values,
            "DISCORDBOT_AWL_WEBHOOK_URL",
        ),
        sftp_host=_environment_value(env_file_values, "SFTP_HOST"),
        sftp_port=_environment_value(env_file_values, "SFTP_PORT") or "22",
        sftp_username=_environment_value(env_file_values, "SFTP_USERNAME"),
        sftp_password=_environment_value(env_file_values, "SFTP_PASSWORD"),
        sftp_known_hosts=_environment_value(env_file_values, "SFTP_KNOWN_HOSTS"),
        sftp_remote_path=_environment_value(env_file_values, "SFTP_REMOTE_PATH"),
    )
    args = parser.parse_args()

    if not args.version:
        parser.error("--version is required")

    missing = []
    if not args.tcadmin_api_key:
        missing.append("TCADMIN_API_KEY")
    if not args.tcadmin_service_id:
        missing.append("TCADMIN_SERVICE_ID")
    required_sftp = {
        "SFTP_HOST": args.sftp_host,
        "SFTP_USERNAME": args.sftp_username,
        "SFTP_PASSWORD": args.sftp_password,
        "SFTP_KNOWN_HOSTS": args.sftp_known_hosts,
        "SFTP_REMOTE_PATH": args.sftp_remote_path,
        "DISCORDBOT_AWL_WEBHOOK_URL": (
            args.discordbot_awl_webhook_url
        ),
    }
    missing.extend(
        key for key, value in required_sftp.items() if not value
    )
    if missing:
        parser.error("missing required configuration: " + ", ".join(missing))
    try:
        args.sftp_port = positive_integer(args.sftp_port)
    except argparse.ArgumentTypeError as error:
        parser.error(f"SFTP_PORT {error}")
    if args.sftp_port > 65535:
        parser.error("SFTP_PORT must be between 1 and 65535")
    return args


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
    )
    try:
        response = requests.post(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        raise RuntimeError(f"Discord webhook request failed: {error}") from error
    if response.status_code not in (200, 204):
        raise RuntimeError(
            f"Discord webhook returned unexpected status {response.status_code}"
        )


def send_game_message(webhook_url, message):
    webhook_message(webhook_url, f"!message {message}")


def _wait_for_countdown_delay(cancel_event, seconds):
    if cancel_event.wait(seconds):
        return False
    return True


def send_countdown(webhook_url, package_version, cancel_event):
    for index, minutes in enumerate(NOTICE_MINUTES):
        if index and not _wait_for_countdown_delay(
            cancel_event,
            NOTICE_DELAYS_SECONDS[index - 1],
        ):
            send_game_message(
                webhook_url,
                f"The restart for modpack {package_version} was cancelled "
                "because the update could not be prepared.",
            )
            return False
        unit = "minute" if minutes == 1 else "minutes"
        send_game_message(
            webhook_url,
            f"Server restarting in {minutes} {unit}.",
        )

    if not _wait_for_countdown_delay(
        cancel_event,
        NOTICE_DELAYS_SECONDS[-1],
    ):
        send_game_message(
            webhook_url,
            f"The restart for modpack {package_version} was cancelled "
            "because the update could not be prepared.",
        )
        return False
    return True


def announce_restart_and_save(webhook_url, package_version):
    send_game_message(
        webhook_url,
        f"The server is restarting now to install modpack {package_version}.",
    )
    webhook_message(webhook_url, "!save")
    time.sleep(SAVE_SETTLE_SECONDS)


class TcAdminClient:
    def __init__(
        self,
        panel_url,
        api_key,
        service_id,
        api_key_header="api_key",
    ):
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
        if not service_id or not service_id.isdigit():
            raise ValueError("TCADMIN_SERVICE_ID must be numeric")
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", api_key_header):
            raise ValueError("GTX_API_KEY_HEADER must be a valid HTTP header")

        self.panel_url = panel_url.rstrip("/")
        self.api_key = api_key
        self.api_key_header = api_key_header
        self.service_id = service_id
        self.service_path = (
            f"/api/service/{urllib.parse.quote(service_id, safe='')}"
        )

    def _request(self, method, path, fields=None):
        url = self.panel_url + path
        headers = {
            "Accept": "application/json",
            self.api_key_header: self.api_key,
        }
        try:
            if method == "POST":
                response = requests.post(
                    url,
                    headers=headers,
                    files={
                        key: (None, value)
                        for key, value in (fields or {}).items()
                    },
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            else:
                response = requests.get(
                    url,
                    headers=headers,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            response.raise_for_status()
        except requests.HTTPError as error:
            response = error.response
            detail = response.text[:500] if response is not None else str(error)
            raise RuntimeError(
                f"TCAdmin {method} request to {url} failed with HTTP "
                f"{response.status_code if response is not None else 'error'}"
                f": {detail}"
            ) from error
        except requests.RequestException as error:
            raise RuntimeError(
                f"TCAdmin {method} request to {url} failed: {error}"
            ) from error

        try:
            result = response.json()
        except ValueError as error:
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

    def restart_service(self):
        self._request(
            "POST",
            f"{self.service_path}/restart",
            fields={"reason": "Starfinders modpack update"},
        )


def _stage_packages(args, cancel_event):
    return deploy_to_sftp(
        "thunderstore.toml",
        args.sftp_host,
        args.sftp_port,
        args.sftp_username,
        args.sftp_password,
        args.sftp_known_hosts,
        args.sftp_remote_path,
        cancel_event=cancel_event,
    )


def _prepare_update(args, cancel_event):
    staged = None
    with ThreadPoolExecutor(max_workers=2) as executor:
        deployment_future = executor.submit(_stage_packages, args, cancel_event)
        countdown_future = executor.submit(
            send_countdown,
            args.discordbot_awl_webhook_url,
            args.version,
            cancel_event,
        )
        futures = (deployment_future, countdown_future)
        done, _ = wait(futures, return_when=FIRST_EXCEPTION)
        errors = [
            future.exception()
            for future in done
            if not future.cancelled() and future.exception() is not None
        ]
        if errors:
            cancel_event.set()
            error = errors[0]
            for future in futures:
                try:
                    result = future.result()
                except OPERATION_ERRORS as future_error:
                    if future_error not in errors:
                        errors.append(future_error)
                else:
                    if future is deployment_future:
                        staged = result
            if len(errors) > 1:
                logger.error(
                    "Additional update preparation failure(s): %s",
                    "; ".join(str(item) for item in errors[1:]),
                )
            if staged is not None:
                try:
                    discard_staged_deployment(
                        staged,
                        args.sftp_host,
                        args.sftp_port,
                        args.sftp_username,
                        args.sftp_password,
                        args.sftp_known_hosts,
                    )
                except (
                    OSError,
                    EOFError,
                    RuntimeError,
                    ValueError,
                    paramiko.SFTPError,
                    paramiko.SSHException,
                ) as cleanup_error:
                    logger.error(
                        "Could not remove staged package files: %s",
                        cleanup_error,
                    )
            raise error

        staged = deployment_future.result()
        countdown_complete = countdown_future.result()
        if not countdown_complete:
            raise RuntimeError("Restart countdown was cancelled")
    return staged


def report_failure(webhook_url, package_version, restart_attempted):
    if restart_attempted:
        logger.error(
            "Automatic mod update for modpack %s failed after a server "
            "restart was requested. Check TCAdmin and the server status.",
            package_version,
        )
        return

    try:
        send_game_message(
            webhook_url,
            f"Automatic mod update for modpack {package_version} failed "
            "before a restart was requested. The server was left running.",
        )
    except (OSError, RuntimeError, ValueError) as error:
        logger.error("Could not send the failure notice: %s", error)


def update_server(args):
    webhook_url = args.discordbot_awl_webhook_url
    validate_webhook_url(webhook_url)
    client = TcAdminClient(
        args.tcadmin_panel_url,
        args.tcadmin_api_key,
        args.tcadmin_service_id,
        args.tcadmin_api_key_header,
    )
    promotion_attempted = False
    restart_attempted = False
    staged = None
    cancel_event = threading.Event()
    try:
        status = client.service_status()
        if status != 4:
            raise RuntimeError(
                "Refusing to update because the TCAdmin service is not "
                f"running (ServiceStatus={status}, expected 4)"
            )

        logger.info(
            "Starting package preparation and the 10-minute restart "
            "countdown in parallel."
        )
        staged = _prepare_update(args, cancel_event)
        logger.info(
            "Promoting staged plugin files while the server is running."
        )
        promotion_attempted = True
        promote_staged_deployment(
            staged,
            args.sftp_host,
            args.sftp_port,
            args.sftp_username,
            args.sftp_password,
            args.sftp_known_hosts,
            args.sftp_remote_path,
        )

        announce_restart_and_save(webhook_url, args.version)

        logger.info("Restarting TCAdmin service %s.", args.tcadmin_service_id)
        restart_attempted = True
        client.restart_service()
        client.wait_for_status(1)
        client.wait_for_status(4)
        send_game_message(
            webhook_url,
            f"The server is back online with modpack {args.version}.",
        )
    except OPERATION_ERRORS:
        cancel_event.set()
        if staged is not None and not promotion_attempted:
            try:
                discard_staged_deployment(
                    staged,
                    args.sftp_host,
                    args.sftp_port,
                    args.sftp_username,
                    args.sftp_password,
                    args.sftp_known_hosts,
                )
            except (
                OSError,
                EOFError,
                RuntimeError,
                ValueError,
                paramiko.SFTPError,
                paramiko.SSHException,
            ) as cleanup_error:
                logger.error(
                    "Could not remove staged package files: %s",
                    cleanup_error,
                )
        report_failure(webhook_url, args.version, restart_attempted)
        raise


def main():
    args = parse_args()
    update_server(args)


if __name__ == "__main__":
    main()
