
"""Wait for a package version in Thunderstore's listing index."""

import argparse
import gzip
import hashlib
import json
import logging
import re
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

INDEX_URL = (
    "https://thunderstore.io/c/valheim/api/v1/package-listing-index/"
)
DEFAULT_INTERVAL_MINUTES = 10
DEFAULT_MAX_CHECKS = 50
FULL_REFRESH_AFTER_SECONDS = 4 * 60 * 60
TIMEOUT_SECONDS = 30

BLOB_PATTERN = re.compile(
    r'https://[^"\\\s]*blob-storage[^"\\\s]*'
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def fetch(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Thunderstore-Version-Checker/1.0",
            "Accept": "application/json, */*",
        },
    )

    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        data = response.read()

    if data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)

    return data


def get_blob_urls():
    data = fetch(INDEX_URL)
    urls = sorted(set(BLOB_PATTERN.findall(data.decode("utf-8"))))

    if not urls:
        raise ValueError("No blob URLs found in the listing index")

    return urls


def walk_dicts(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def contains_version(data, package_full_name, version):
    document = json.loads(data)
    expected_version_name = f"{package_full_name}-{version}"

    for package in walk_dicts(document):
        if package.get("full_name") != package_full_name:
            continue

        versions = package.get("versions", [])
        if not isinstance(versions, list):
            continue

        for item in versions:
            if not isinstance(item, dict):
                continue
            if item.get("version_number") != version:
                continue
            if item.get("is_active") is False:
                continue

            full_name = item.get("full_name")
            if full_name is not None and full_name != expected_version_name:
                continue

            return True

    return False


def cache_path(cache_dir, url):
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return cache_dir / f"{key}.json"


def inspect_blob(url, cache_dir, package_full_name, version, force=False):
    path = cache_path(cache_dir, url)

    try:
        if force or not path.exists():
            data = fetch(url)
            # Write only successfully downloaded data to the cache.
            path.write_bytes(data)
        else:
            data = path.read_bytes()

        return contains_version(data, package_full_name, version)

    except (
        OSError,
        urllib.error.URLError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        ValueError,
    ) as error:
        logger.warning("Could not inspect blob %s: %s", url, error)
        return None


def scan_urls(urls, cache_dir, package_full_name, version, force=False):
    """Return True if found, otherwise False; log individual failures."""
    failures = 0

    for number, url in enumerate(urls, start=1):
        result = inspect_blob(
            url, cache_dir, package_full_name, version, force=force
        )

        if result is True:
            logger.info(
                "Found %s version %s in listing blob.",
                package_full_name,
                version,
            )
            return True

        if result is None:
            failures += 1

        if number % 100 == 0:
            logger.info("Inspected %s/%s blobs.", number, len(urls))

    if failures:
        logger.warning(
            "%s blob(s) could not be inspected; a later check will retry them.",
            failures,
        )

    return False


def parse_args():
    parser = argparse.ArgumentParser(
        description="Wait for a package version in Thunderstore listing data."
    )
    parser.add_argument("--owner", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=DEFAULT_INTERVAL_MINUTES,
    )
    parser.add_argument(
        "--max-checks",
        type=int,
        default=DEFAULT_MAX_CHECKS,
    )

    args = parser.parse_args()

    if args.interval_minutes < 1:
        parser.error("--interval-minutes must be at least 1")
    if args.max_checks < 1:
        parser.error("--max-checks must be at least 1")

    return args


def main():
    args = parse_args()
    package_full_name = f"{args.owner}-{args.name}"
    interval_seconds = args.interval_minutes * 60
    started_at = time.monotonic()
    full_refresh_done = False

    logger.info(
        "Waiting for %s version %s; checking every %s minutes.",
        package_full_name,
        args.version,
        args.interval_minutes,
    )

    # Cache lasts for this run. GitHub-hosted runners start fresh each job.
    with tempfile.TemporaryDirectory(prefix="thunderstore-check-") as temp:
        cache_dir = Path(temp)

        for check in range(1, args.max_checks + 1):
            try:
                urls = get_blob_urls()

                elapsed = time.monotonic() - started_at
                do_full_refresh = (
                    elapsed >= FULL_REFRESH_AFTER_SECONDS
                    and not full_refresh_done
                )

                if do_full_refresh:
                    logger.info(
                        "Four-hour safety sweep: refreshing all %s blobs.",
                        len(urls),
                    )
                    found = scan_urls(
                        urls,
                        cache_dir,
                        package_full_name,
                        args.version,
                        force=True,
                    )
                    full_refresh_done = True

                elif check == 1:
                    # Establish the initial baseline.
                    logger.info(
                        "Initial scan of %s listing blobs.", len(urls)
                    )
                    found = scan_urls(
                        urls, cache_dir, package_full_name, args.version
                    )

                else:
                    # New content-addressed URLs represent new blob contents.
                    new_urls = [
                        url for url in urls
                        if not cache_path(cache_dir, url).exists()
                    ]
                    logger.info(
                        "Check %s/%s: %s new blob(s) to inspect.",
                        check,
                        args.max_checks,
                        len(new_urls),
                    )
                    found = scan_urls(
                        new_urls,
                        cache_dir,
                        package_full_name,
                        args.version,
                    )

                if found:
                    logger.info(
                        "SUCCESS: %s %s is listed on Thunderstore.",
                        package_full_name,
                        args.version,
                    )
                    return 0

            except (
                OSError,
                urllib.error.URLError,
                json.JSONDecodeError,
                UnicodeDecodeError,
                ValueError,
                gzip.BadGzipFile,
            ) as error:
                logger.warning(
                    "Check %s/%s failed; will retry: %s",
                    check,
                    args.max_checks,
                    error,
                )

            if check < args.max_checks:
                time.sleep(interval_seconds)

    logger.error(
        "TIMEOUT: %s %s was not confirmed after %s checks.",
        package_full_name,
        args.version,
        args.max_checks,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
