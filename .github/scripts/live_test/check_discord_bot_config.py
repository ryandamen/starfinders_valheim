"""Send a live DiscordBot_AWL message to verify the integration."""

import argparse
import os
from pathlib import Path

from update_valheim_server import (
    read_env_file,
    send_game_message,
    validate_webhook_url,
)

DEFAULT_MESSAGE = "DiscordBot_AWL configuration test from Starfinders."
WEBHOOK_ENV_NAME = "DISCORDBOT_AWL_WEBHOOK_URL"


def load_webhook_url(env_file_path):
    env_file_values = read_env_file(env_file_path) if env_file_path else {}
    return os.environ.get(WEBHOOK_ENV_NAME) or env_file_values.get(
        WEBHOOK_ENV_NAME
    )


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Send a test !message command through the DiscordBot_AWL webhook."
        )
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(__file__).resolve().parents[3] / ".env",
        help="KEY=VALUE file (defaults to the repository .env).",
    )
    parser.add_argument(
        "--message",
        default=DEFAULT_MESSAGE,
        help="Text to display in the Valheim server chat.",
    )
    args = parser.parse_args()
    if not args.message.strip():
        parser.error("--message must not be empty")
    return args


def main():
    args = parse_args()
    try:
        webhook_url = load_webhook_url(args.env_file)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    if not webhook_url:
        raise SystemExit(
            f"{WEBHOOK_ENV_NAME} is missing from the environment and "
            f"{args.env_file}"
        )

    try:
        validate_webhook_url(webhook_url)
    except ValueError as error:
        raise SystemExit(str(error)) from error

    print("This will send the following command to the AWL admin channel:")
    print(f"!message {args.message}")
    print("If AWL accepts it, the text will be sent to the live server chat.")
    send_game_message(webhook_url, args.message)
    print(
        "Discord accepted the webhook request. Check the AWL command channel "
        "and live server chat to confirm the bot processed it."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
