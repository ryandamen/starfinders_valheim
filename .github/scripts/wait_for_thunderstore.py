"""Poll Thunderstore until a package version becomes active."""

import argparse
import json
import logging
import time
import urllib.request

FEED_URL = "https://thunderstore.io/c/valheim/api/v1/package/"
DEFAULT_INTERVAL_MINUTES = 5
DEFAULT_MAX_CHECKS = 50

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Wait for a package version to become active on Thunderstore."
    )
    parser.add_argument("--owner", required=True, help="Thunderstore package owner")
    parser.add_argument("--name", required=True, help="Thunderstore package name")
    parser.add_argument("--version", required=True, help="Package version to wait for")
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=DEFAULT_INTERVAL_MINUTES,
        help=f"Minutes between checks (default: {DEFAULT_INTERVAL_MINUTES})",
    )
    parser.add_argument(
        "--max-checks",
        type=int,
        default=DEFAULT_MAX_CHECKS,
        help=f"Maximum number of checks (default: {DEFAULT_MAX_CHECKS})",
    )
    args = parser.parse_args()
    if args.interval_minutes < 1:
        parser.error("--interval-minutes must be at least 1")
    if args.max_checks < 1:
        parser.error("--max-checks must be at least 1")
    return args


def main():
    args = parse_args()
    interval_seconds = args.interval_minutes * 60
    last_check_at = time.monotonic()

    logger.info(
        "Waiting for %s-%s %s to become active in the Thunderstore feed. "
        "Checking every %s minutes for up to %s checks.",
        args.owner,
        args.name,
        args.version,
        args.interval_minutes,
        args.max_checks,
    )

    for check in range(args.max_checks):
        elapsed = time.monotonic() - last_check_at
        time.sleep(max(0, interval_seconds - elapsed))
        last_check_at = time.monotonic()

        try:
            with urllib.request.urlopen(FEED_URL, timeout=30) as response:
                packages = json.load(response)
            if not isinstance(packages, list):
                raise ValueError("Thunderstore package feed did not return a list")

            available = any(
                package.get("owner") == args.owner
                and package.get("name") == args.name
                and any(
                    package_version.get("version_number") == args.version
                    and package_version.get("is_active") is True
                    for package_version in package.get("versions", [])
                )
                for package in packages
            )
        except (OSError, json.JSONDecodeError, ValueError) as error:
            logger.warning(
                "Check %s/%s: Thunderstore feed check failed: %s",
                check + 1,
                args.max_checks,
                error,
            )
        else:
            if available:
                logger.info(
                    "%s-%s %s is active in the Thunderstore feed.",
                    args.owner,
                    args.name,
                    args.version,
                )
                return
            logger.info(
                "Check %s/%s: version is not active in the Thunderstore feed yet.",
                check + 1,
                args.max_checks,
            )

        if check + 1 == args.max_checks:
            logger.error(
                "Timed out waiting for %s-%s %s to become active in the "
                "Thunderstore feed after %s checks spaced %s minutes apart.",
                args.owner,
                args.name,
                args.version,
                args.max_checks,
                args.interval_minutes,
            )
            raise SystemExit(1)


if __name__ == "__main__":
    main()
