"""Resolve Thunderstore dependencies and stage their plugin files over SFTP."""

from dataclasses import dataclass
from io import BytesIO
import errno
import hashlib
import logging
import os
from pathlib import Path, PurePosixPath
import posixpath
import stat
import tempfile
import threading
import urllib.parse
import uuid
import zipfile

import paramiko
import requests
import tomllib

PACKAGE_INDEX_URL = "https://thunderstore.io/c/valheim/api/v1/package/"
REQUEST_TIMEOUT_SECONDS = 60
SFTP_TIMEOUT_SECONDS = 30
PLUGIN_PATH = PurePosixPath("BepInEx", "plugins")
METADATA_FILES = {
    "changelog.md",
    "icon.png",
    "license",
    "license.md",
    "license.txt",
    "notice.md",
    "readme.md",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PackageVersion:
    namespace: str
    name: str
    version: str
    full_name: str
    download_url: str
    dependencies: tuple[str, ...]


@dataclass(frozen=True)
class StagedDeployment:
    remote_stage_path: str
    files: tuple[str, ...]
    staged_files: tuple[str, ...] | None = None
    replace_mods: tuple[str, ...] = ()
    remote_backup_path: str | None = None


class ThunderstoreClient:
    def __init__(self, session=None):
        self.session = session or requests.Session()
        self._versions = None

    def load_versions(self):
        if self._versions is not None:
            return self._versions

        try:
            response = self.session.get(
                PACKAGE_INDEX_URL,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            packages = response.json()
        except (requests.RequestException, ValueError) as error:
            raise RuntimeError(
                f"Could not load Thunderstore's Valheim package index: {error}"
            ) from error

        if not isinstance(packages, list):
            raise RuntimeError("Thunderstore package index was not a list")

        versions = {}
        for package in packages:
            if not isinstance(package, dict):
                continue
            namespace = package.get("owner")
            name = package.get("name")
            package_versions = package.get("versions")
            if (
                not isinstance(namespace, str)
                or not isinstance(name, str)
                or not isinstance(package_versions, list)
            ):
                continue

            for item in package_versions:
                if not isinstance(item, dict):
                    continue
                full_name = item.get("full_name")
                version = item.get("version_number")
                download_url = item.get("download_url")
                dependencies = item.get("dependencies", [])
                if (
                    not isinstance(full_name, str)
                    or not isinstance(version, str)
                    or not isinstance(download_url, str)
                    or not isinstance(dependencies, list)
                    or not all(isinstance(value, str) for value in dependencies)
                ):
                    continue
                if item.get("is_active") is False:
                    continue
                package_version = PackageVersion(
                    namespace=namespace,
                    name=name,
                    version=version,
                    full_name=full_name,
                    download_url=download_url,
                    dependencies=tuple(dependencies),
                )
                versions[full_name.casefold()] = package_version

        if not versions:
            raise RuntimeError(
                "Thunderstore package index contained no usable versions"
            )
        self._versions = versions
        return versions

    def resolve(self, dependencies):
        versions = self.load_versions()
        selected = {}
        direct_identities = set()
        direct_packages = []

        for package_name, version in dependencies.items():
            if not isinstance(package_name, str) or not isinstance(version, str):
                raise ValueError(
                    "Thunderstore dependency names and versions must be strings"
                )
            namespace, separator, name = package_name.partition("-")
            if not separator or not namespace or not name:
                raise ValueError(
                    f"Invalid Thunderstore dependency name {package_name!r}"
                )
            full_name = f"{namespace}-{name}-{version}"
            package = versions.get(full_name.casefold())
            if package is None:
                raise RuntimeError(
                    f"Manifest dependency {full_name!r} was not found or is "
                    "inactive on Thunderstore"
                )
            identity = (package.namespace.casefold(), package.name.casefold())
            existing = selected.get(identity)
            if existing is not None:
                if existing.version != package.version:
                    raise RuntimeError(
                        f"Conflicting versions requested for "
                        f"{package.namespace}-{package.name}: "
                        f"{existing.version} and {package.version}"
                    )
            else:
                selected[identity] = package
                direct_packages.append(package)
                direct_identities.add(identity)

        ordered = list(direct_packages)
        visited = set()

        def resolve_tree(package):
            identity = (package.namespace.casefold(), package.name.casefold())
            if identity in visited:
                return
            visited.add(identity)
            for dependency in package.dependencies:
                child = versions.get(dependency.casefold())
                if child is None:
                    raise RuntimeError(
                        f"{package.full_name} requires missing or inactive "
                        f"Thunderstore dependency {dependency!r}"
                    )
                child_identity = (
                    child.namespace.casefold(),
                    child.name.casefold(),
                )
                existing = selected.get(child_identity)
                if existing is None:
                    selected[child_identity] = child
                    ordered.append(child)
                    existing = child
                elif existing.version != child.version:
                    if child_identity not in direct_identities:
                        raise RuntimeError(
                            f"Conflicting transitive versions requested for "
                            f"{child.namespace}-{child.name}: "
                            f"{existing.version} and {child.version}"
                        )
                    logger.info(
                        "Using manifest-pinned %s-%s %s instead of "
                        "transitive version %s.",
                        existing.namespace,
                        existing.name,
                        existing.version,
                        child.version,
                    )
                resolve_tree(existing)

        for package in direct_packages:
            resolve_tree(package)

        return ordered

    def download(self, package):
        parsed_url = urllib.parse.urlsplit(package.download_url)
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname != "thunderstore.io"
        ):
            raise RuntimeError(
                f"Thunderstore returned an unexpected download URL for "
                f"{package.full_name}"
            )
        try:
            response = self.session.get(
                package.download_url,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            raise RuntimeError(
                f"Could not download Thunderstore package "
                f"{package.full_name}: {error}"
            ) from error
        return response.content


def load_manifest_dependencies(manifest_path):
    try:
        with Path(manifest_path).open("rb") as manifest_file:
            document = tomllib.load(manifest_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(
            f"Could not read Thunderstore manifest {manifest_path}: {error}"
        ) from error

    try:
        dependencies = document["package"]["dependencies"]
    except (KeyError, TypeError) as error:
        raise ValueError(
            "thunderstore.toml must define [package.dependencies]"
        ) from error
    if not isinstance(dependencies, dict) or not dependencies:
        raise ValueError("thunderstore.toml has no package dependencies")
    return dependencies


def _plugin_relative_path(package, filename):
    filename = filename.replace("\\", "/")
    path = PurePosixPath(filename)
    if (
        path.is_absolute()
        or (path.parts and path.parts[0].endswith(":"))
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise RuntimeError(
            f"Package archive contains an unsafe path: {filename!r}"
        )
    if path.parts and path.parts[0] == "BepInEx":
        if path.parts[:2] != PLUGIN_PATH.parts or len(path.parts) == 2:
            return None
        return PurePosixPath(*path.parts[2:])

    if package.name.casefold().startswith("bepinexpack"):
        return None

    package_directory = f"{package.namespace}-{package.name}"
    if (
        not package_directory
        or package_directory in {".", ".."}
        or "/" in package_directory
        or "\\" in package_directory
    ):
        raise RuntimeError(
            f"Thunderstore package has an invalid name: {package.full_name}"
        )
    return PurePosixPath(package_directory, *path.parts)


def extract_plugin_files(package, archive_bytes, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    written = {}

    try:
        archive = zipfile.ZipFile(BytesIO(archive_bytes))
    except (OSError, zipfile.BadZipFile) as error:
        raise RuntimeError(
            f"Thunderstore download for {package.full_name} is not a valid zip"
        ) from error

    with archive:
        for item in archive.infolist():
            relative_path = _plugin_relative_path(package, item.filename)
            if relative_path is None or item.is_dir():
                continue
            if relative_path.name.casefold() in METADATA_FILES:
                continue
            mode = item.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise RuntimeError(
                    f"Package {package.full_name} contains a symlink: "
                    f"{item.filename}"
                )

            data = archive.read(item)
            relative_name = relative_path.as_posix()
            target = destination.joinpath(*relative_path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            previous = written.get(relative_name)
            if previous is not None and previous != data:
                raise RuntimeError(
                    f"Multiple packages contain different files at "
                    f"BepInEx/plugins/{relative_name}"
                )
            if previous is None:
                target.write_bytes(data)
                written[relative_name] = data

    return written


def prepare_plugin_files(manifest_path, destination, cancel_event=None):
    dependencies = load_manifest_dependencies(manifest_path)
    client = ThunderstoreClient()
    packages = client.resolve(dependencies)
    logger.info(
        "Resolved %s Thunderstore package(s), including transitive dependencies.",
        len(packages),
    )

    files = {}
    for package in packages:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Package preparation was cancelled")
        logger.info("Downloading %s.", package.full_name)
        package_files = extract_plugin_files(
            package,
            client.download(package),
            destination,
        )
        for relative_name, data in package_files.items():
            previous = files.get(relative_name)
            if previous is not None and previous != data:
                raise RuntimeError(
                    f"Multiple packages contain different files at "
                    f"BepInEx/plugins/{relative_name}"
                )
            files[relative_name] = data

    if not files:
        raise RuntimeError(
            "Thunderstore packages contained no BepInEx/plugins files"
        )
    return tuple(sorted(files))


def _validate_remote_path(path, label):
    remote_path = PurePosixPath(path)
    if (
        not remote_path.is_absolute()
        or len(remote_path.parts) < 2
        or any(part in {"", ".", ".."} for part in remote_path.parts)
    ):
        raise ValueError(f"{label} must be a normalized absolute POSIX path")
    return remote_path.as_posix()


def _validate_relative_plugin_path(path):
    if not isinstance(path, str) or "\\" in path:
        raise ValueError(f"Invalid managed plugin path {path!r}")
    relative_path = PurePosixPath(path)
    if (
        not relative_path.parts
        or relative_path.is_absolute()
        or relative_path.as_posix() != path
        or any(part in {"", ".", ".."} for part in relative_path.parts)
    ):
        raise ValueError(f"Invalid managed plugin path {path!r}")
    return relative_path


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remote_sha256_file(sftp, path):
    try:
        with sftp.open(path, "rb") as remote_file:
            digest = hashlib.sha256()
            for chunk in iter(lambda: remote_file.read(1024 * 1024), b""):
                digest.update(chunk)
            return digest.hexdigest()
    except OSError as error:
        if getattr(error, "errno", None) == errno.ENOENT:
            return None
        raise


def _has_remote_files(sftp, remote_directory, relative_names):
    for relative_name in relative_names:
        relative_path = _validate_relative_plugin_path(relative_name)
        remote_path = posixpath.join(remote_directory, *relative_path.parts)
        try:
            attributes = sftp.lstat(remote_path)
        except OSError as error:
            if getattr(error, "errno", None) == errno.ENOENT:
                return False
            raise
        if not stat.S_ISREG(attributes.st_mode):
            return False
    return True


def _remove_existing_file(sftp, path):
    try:
        attributes = sftp.lstat(path)
    except OSError as error:
        if getattr(error, "errno", None) == errno.ENOENT:
            return
        raise
    if stat.S_ISDIR(attributes.st_mode):
        raise RuntimeError(f"Cannot replace directory with plugin file: {path}")
    sftp.remove(path)


def _connect_sftp(host, port, username, password, known_hosts):
    if not all((host, username, password, known_hosts)):
        raise ValueError(
            "SFTP_HOST, SFTP_USERNAME, SFTP_PASSWORD, and "
            "SFTP_KNOWN_HOSTS are required"
        )

    ssh = paramiko.SSHClient()
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
        ) as known_hosts_file:
            known_hosts_file.write(known_hosts.rstrip() + "\n")
            known_hosts_path = known_hosts_file.name
        try:
            ssh.load_host_keys(known_hosts_path)
        finally:
            os.unlink(known_hosts_path)

        host_key_name = host if int(port) == 22 else f"[{host}]:{port}"
        if host_key_name not in ssh.get_host_keys():
            raise ValueError(
                f"SFTP_KNOWN_HOSTS has no verified key for {host_key_name}"
            )
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        ssh.connect(
            hostname=host,
            port=int(port),
            username=username,
            password=password,
            timeout=SFTP_TIMEOUT_SECONDS,
            auth_timeout=SFTP_TIMEOUT_SECONDS,
            banner_timeout=SFTP_TIMEOUT_SECONDS,
            allow_agent=False,
            look_for_keys=False,
        )
        return ssh, ssh.open_sftp()
    except (OSError, paramiko.SSHException, ValueError):
        ssh.close()
        raise


def _ensure_remote_directory(sftp, path):
    path = PurePosixPath(path)
    current = PurePosixPath("/")
    for part in path.parts[1:]:
        current = current / part
        try:
            sftp.stat(current.as_posix())
        except OSError as error:
            if getattr(error, "errno", None) != errno.ENOENT:
                raise
            sftp.mkdir(current.as_posix())


def _remove_remote_tree(sftp, path):
    try:
        attributes = sftp.listdir_attr(path)
    except OSError as error:
        if getattr(error, "errno", None) == errno.ENOENT:
            return
        raise

    for item in attributes:
        child = posixpath.join(path, item.filename)
        if stat.S_ISDIR(item.st_mode):
            _remove_remote_tree(sftp, child)
        else:
            sftp.remove(child)
    sftp.rmdir(path)


def _remote_files(sftp, path):
    files = []
    for item in sftp.listdir_attr(path):
        child = posixpath.join(path, item.filename)
        if stat.S_ISDIR(item.st_mode):
            files.extend(_remote_files(sftp, child))
        else:
            files.append(child)
    return files


def _backup_remote_file(sftp, source, backup, backups):
    try:
        attributes = sftp.lstat(source)
    except OSError as error:
        if getattr(error, "errno", None) == errno.ENOENT:
            return
        raise
    if not stat.S_ISREG(attributes.st_mode):
        raise RuntimeError(f"Cannot replace non-file plugin path: {source}")
    _ensure_remote_directory(sftp, posixpath.dirname(backup))
    sftp.rename(source, backup)
    backups.append((backup, source))


def stage_plugin_files(
    local_root,
    files,
    host,
    port,
    username,
    password,
    known_hosts,
    remote_directory,
    cancel_event=None,
):
    remote_directory = _validate_remote_path(
        remote_directory,
        "SFTP_REMOTE_PATH",
    )
    parent = posixpath.dirname(remote_directory)
    stage_path = posixpath.join(
        parent,
        f".starfinders-staging-{uuid.uuid4().hex}",
    )
    backup_path = f"{stage_path}-backup"
    ssh, sftp = _connect_sftp(host, port, username, password, known_hosts)
    try:
        _ensure_remote_directory(sftp, parent)
        sftp.mkdir(stage_path)
        sftp.mkdir(backup_path)
        staged_files = []
        replace_mods = []
        file_groups = {}
        for relative_name in files:
            relative_path = _validate_relative_plugin_path(relative_name)
            if len(relative_path.parts) > 1:
                group_name = relative_path.parts[0]
            else:
                group_name = None
            file_groups.setdefault(group_name, []).append(relative_name)

        for group_name, group_files in file_groups.items():
            unchanged = False
            manifest_name = (
                f"{group_name}/manifest.json"
                if group_name is not None
                else None
            )
            if manifest_name in group_files:
                local_manifest = Path(local_root).joinpath(
                    *_validate_relative_plugin_path(manifest_name).parts
                )
                remote_manifest = posixpath.join(
                    remote_directory,
                    *PurePosixPath(manifest_name).parts,
                )
                unchanged = (
                    _remote_sha256_file(sftp, remote_manifest)
                    == _sha256_file(local_manifest)
                    and _has_remote_files(
                        sftp,
                        remote_directory,
                        group_files,
                    )
                )
                if unchanged:
                    logger.info(
                        "Skipping unchanged mod %s (manifest matches).",
                        group_name,
                    )

            if unchanged:
                continue

            if group_name is not None:
                replace_mods.append(group_name)
            for relative_name in group_files:
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("SFTP upload was cancelled")
                relative_path = _validate_relative_plugin_path(relative_name)
                local_path = Path(local_root).joinpath(*relative_path.parts)
                remote_path = posixpath.join(stage_path, *relative_path.parts)
                remote_parent = posixpath.dirname(remote_path)
                _ensure_remote_directory(sftp, remote_parent)
                logger.info("Staging BepInEx/plugins/%s.", relative_name)
                sftp.put(str(local_path), remote_path)
                staged_files.append(relative_name)
        return StagedDeployment(
            stage_path,
            tuple(files),
            tuple(staged_files),
            tuple(replace_mods),
            backup_path,
        )
    except (
        OSError,
        EOFError,
        paramiko.SFTPError,
        paramiko.SSHException,
        RuntimeError,
        ValueError,
    ):
        try:
            _remove_remote_tree(sftp, stage_path)
            _remove_remote_tree(sftp, backup_path)
        except (
            OSError,
            EOFError,
            paramiko.SFTPError,
            paramiko.SSHException,
        ):
            logger.exception("Could not clean failed SFTP staging directory.")
        raise
    finally:
        sftp.close()
        ssh.close()


def discard_staged_deployment(
    staged,
    host,
    port,
    username,
    password,
    known_hosts,
):
    ssh, sftp = _connect_sftp(host, port, username, password, known_hosts)
    try:
        _remove_remote_tree(sftp, staged.remote_stage_path)
        if staged.remote_backup_path is not None:
            _remove_remote_tree(sftp, staged.remote_backup_path)
    finally:
        sftp.close()
        ssh.close()


def promote_staged_deployment(
    staged,
    host,
    port,
    username,
    password,
    known_hosts,
    remote_directory,
):
    remote_directory = _validate_remote_path(
        remote_directory,
        "SFTP_REMOTE_PATH",
    )
    ssh, sftp = _connect_sftp(host, port, username, password, known_hosts)
    try:
        _ensure_remote_directory(sftp, remote_directory)
        files_to_promote = (
            staged.files if staged.staged_files is None else staged.staged_files
        )
        replaced_mods = set(staged.replace_mods)
        if staged.staged_files is None:
            replaced_mods.update(
                relative_path.parts[0]
                for relative_path in (
                    _validate_relative_plugin_path(name)
                    for name in files_to_promote
                )
                if len(relative_path.parts) > 1
            )

        existing_mod_files = {}
        for mod_name in sorted(replaced_mods):
            target_mod_path = posixpath.join(
                remote_directory,
                mod_name,
            )
            try:
                attributes = sftp.lstat(target_mod_path)
            except OSError as error:
                if getattr(error, "errno", None) != errno.ENOENT:
                    raise
            else:
                if not stat.S_ISDIR(attributes.st_mode):
                    raise RuntimeError(
                        f"Cannot replace non-directory mod folder: "
                        f"{target_mod_path}"
                    )
                existing_mod_files[mod_name] = _remote_files(
                    sftp,
                    target_mod_path,
                )

        backup_path = staged.remote_backup_path or (
            f"{staged.remote_stage_path}-backup"
        )
        backups = []
        installed_files = []
        try:
            for relative_name in files_to_promote:
                relative_path = _validate_relative_plugin_path(relative_name)
                remote_source = posixpath.join(
                    staged.remote_stage_path,
                    *relative_path.parts,
                )
                remote_target = posixpath.join(
                    remote_directory,
                    *relative_path.parts,
                )
                remote_backup = posixpath.join(
                    backup_path,
                    *relative_path.parts,
                )
                _ensure_remote_directory(
                    sftp,
                    posixpath.dirname(remote_target),
                )
                _backup_remote_file(
                    sftp,
                    remote_target,
                    remote_backup,
                    backups,
                )
                sftp.rename(remote_source, remote_target)
                installed_files.append(remote_target)

            new_files = {
                _validate_relative_plugin_path(name).as_posix()
                for name in files_to_promote
            }
            for mod_name, old_files in existing_mod_files.items():
                for old_file in old_files:
                    relative_name = posixpath.relpath(
                        old_file,
                        posixpath.join(remote_directory, mod_name),
                    )
                    managed_name = PurePosixPath(
                        mod_name,
                        relative_name,
                    ).as_posix()
                    if managed_name in new_files:
                        continue
                    relative_path = _validate_relative_plugin_path(
                        managed_name
                    )
                    _backup_remote_file(
                        sftp,
                        old_file,
                        posixpath.join(backup_path, *relative_path.parts),
                        backups,
                    )
        except (
            OSError,
            EOFError,
            paramiko.SFTPError,
            paramiko.SSHException,
            RuntimeError,
            ValueError,
        ) as error:
            rollback_errors = []
            for remote_target in reversed(installed_files):
                try:
                    _remove_existing_file(sftp, remote_target)
                except (
                    OSError,
                    EOFError,
                    paramiko.SFTPError,
                    paramiko.SSHException,
                    RuntimeError,
                    ValueError,
                ) as rollback_error:
                    rollback_errors.append(rollback_error)
            for remote_backup, remote_target in reversed(backups):
                try:
                    _ensure_remote_directory(
                        sftp,
                        posixpath.dirname(remote_target),
                    )
                    _remove_existing_file(sftp, remote_target)
                    sftp.rename(remote_backup, remote_target)
                except (
                    OSError,
                    EOFError,
                    paramiko.SFTPError,
                    paramiko.SSHException,
                    RuntimeError,
                    ValueError,
                ) as rollback_error:
                    rollback_errors.append(rollback_error)
            if rollback_errors:
                logger.error(
                    "SFTP promotion failed and rollback was incomplete: %s",
                    "; ".join(str(item) for item in rollback_errors),
                )
            else:
                try:
                    _remove_remote_tree(sftp, staged.remote_stage_path)
                    _remove_remote_tree(sftp, backup_path)
                except (
                    OSError,
                    EOFError,
                    paramiko.SFTPError,
                    paramiko.SSHException,
                ):
                    logger.exception(
                        "Could not clean staging files after rollback."
                    )
            raise error

        for path in (staged.remote_stage_path, backup_path):
            try:
                _remove_remote_tree(sftp, path)
            except (
                OSError,
                EOFError,
                paramiko.SFTPError,
                paramiko.SSHException,
            ):
                logger.exception(
                    "Promotion succeeded, but SFTP staging cleanup failed "
                    "for %s.",
                    path,
                )
    finally:
        sftp.close()
        ssh.close()


def deploy_to_sftp(
    manifest_path,
    host,
    port,
    username,
    password,
    known_hosts,
    remote_directory,
    cancel_event=None,
):
    with tempfile.TemporaryDirectory(prefix="starfinders-mods-") as temp_dir:
        local_root = Path(temp_dir)
        files = prepare_plugin_files(
            manifest_path,
            local_root,
            cancel_event=cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Package preparation was cancelled")
        return stage_plugin_files(
            local_root,
            files,
            host,
            port,
            username,
            password,
            known_hosts,
            remote_directory,
            cancel_event=cancel_event,
        )
