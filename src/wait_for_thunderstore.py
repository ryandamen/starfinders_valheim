"""Wait until a package version is active in the Thunderstore feed."""

import json
import os
import time
import urllib.request

FEED_URL = "https://thunderstore.io/c/valheim/api/v1/package/"
INTERVAL_SECONDS = 5 * 60
MAX_CHECKS = 50


def main():
    owner = os.environ["PACKAGE_OWNER"]
    name = os.environ["PACKAGE_NAME"]
    version = os.environ["PACKAGE_VERSION"]
    started_at = time.monotonic()

    print(
        f"Waiting for {owner}-{name} {version} to become active in the "
        f"Thunderstore feed. Checking every {INTERVAL_SECONDS // 60} minutes "
        f"for up to {MAX_CHECKS} checks.",
        flush=True,
    )

    for check in range(MAX_CHECKS):
        try:
            with urllib.request.urlopen(FEED_URL, timeout=30) as response:
                packages = json.load(response)
            if not isinstance(packages, list):
                raise ValueError("Thunderstore package feed did not return a list")

            available = any(
                package.get("owner") == owner
                and package.get("name") == name
                and any(
                    package_version.get("version_number") == version
                    and package_version.get("is_active") is True
                    for package_version in package.get("versions", [])
                )
                for package in packages
            )
        except (OSError, json.JSONDecodeError, ValueError) as error:
            print(
                f"Check {check + 1}/{MAX_CHECKS}: Thunderstore feed check failed: {error}",
                flush=True,
            )
        else:
            if available:
                print(
                    f"{owner}-{name} {version} is active in the Thunderstore feed.",
                    flush=True,
                )
                return
            print(
                f"Check {check + 1}/{MAX_CHECKS}: version is not active in the "
                "Thunderstore feed yet.",
                flush=True,
            )

        if check + 1 == MAX_CHECKS:
            raise SystemExit(
                f"Timed out waiting for {owner}-{name} {version} to become active "
                f"in the Thunderstore feed after {MAX_CHECKS} checks spaced "
                f"{INTERVAL_SECONDS // 60} minutes apart."
            )

        next_check_at = started_at + (check + 1) * INTERVAL_SECONDS
        time.sleep(max(0, next_check_at - time.monotonic()))


if __name__ == "__main__":
    main()
