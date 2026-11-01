import io
import posixpath
import sys
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import thunderstore_deployment as deployment


def version(owner, name, version_number, dependencies=(), active=True):
    return {
        "owner": owner,
        "name": name,
        "versions": [
            {
                "full_name": f"{owner}-{name}-{version_number}",
                "version_number": version_number,
                "download_url": (
                    f"https://thunderstore.io/package/download/"
                    f"{owner}/{name}/{version_number}/"
                ),
                "dependencies": list(dependencies),
                "is_active": active,
            }
        ],
    }


class FakeResponse:
    def __init__(self, value=None, content=b""):
        self.value = value
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self.value


class ThunderstoreResolverTests(unittest.TestCase):
    def setUp(self):
        self.index = [
            version(
                "Creator",
                "Root",
                "1.0.0",
                dependencies=("Library-Common-2.0.0",),
            ),
            version("Library", "Common", "2.0.0"),
            version("Library", "Common", "1.0.0"),
        ]
        self.session = mock.Mock()
        self.session.get.return_value = FakeResponse(self.index)
        self.client = deployment.ThunderstoreClient(self.session)

    def test_resolves_pinned_and_transitive_versions(self):
        packages = self.client.resolve({"Creator-Root": "1.0.0"})

        self.assertEqual(
            [package.full_name for package in packages],
            ["Creator-Root-1.0.0", "Library-Common-2.0.0"],
        )
        self.session.get.assert_called_once_with(
            deployment.PACKAGE_INDEX_URL,
            timeout=deployment.REQUEST_TIMEOUT_SECONDS,
        )

    def test_fails_when_a_pinned_package_is_missing_or_inactive(self):
        with self.assertRaisesRegex(RuntimeError, "not found or is inactive"):
            self.client.resolve({"Creator-Missing": "1.0.0"})

    def test_explicit_manifest_pin_overrides_transitive_version(self):
        packages = self.client.resolve(
            {
                "Creator-Root": "1.0.0",
                "Library-Common": "1.0.0",
            }
        )

        self.assertEqual(
            [package.full_name for package in packages],
            ["Creator-Root-1.0.0", "Library-Common-1.0.0"],
        )

    def test_fails_on_conflicting_transitive_only_versions(self):
        self.index.append(
            version(
                "Creator",
                "Other",
                "1.0.0",
                dependencies=("Library-Common-1.0.0",),
            )
        )
        self.session.get.return_value = FakeResponse(self.index)
        client = deployment.ThunderstoreClient(self.session)

        with self.assertRaisesRegex(
            RuntimeError,
            "Conflicting transitive versions",
        ):
            client.resolve(
                {
                    "Creator-Root": "1.0.0",
                    "Creator-Other": "1.0.0",
                }
            )

    def test_fails_when_a_transitive_dependency_is_not_listed(self):
        self.index[0]["versions"][0]["dependencies"] = [
            "Missing-Package-1.0.0"
        ]
        self.session.get.return_value = FakeResponse(self.index)
        client = deployment.ThunderstoreClient(self.session)

        with self.assertRaisesRegex(RuntimeError, "requires missing or inactive"):
            client.resolve({"Creator-Root": "1.0.0"})

    def test_reads_pins_from_thunderstore_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "thunderstore.toml"
            manifest.write_text(
                '[package.dependencies]\n"Creator-Root" = "1.0.0"\n',
                encoding="utf-8",
            )

            self.assertEqual(
                deployment.load_manifest_dependencies(manifest),
                {"Creator-Root": "1.0.0"},
            )


class PackageExtractionTests(unittest.TestCase):
    @staticmethod
    def make_archive(files):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        return output.getvalue()

    def test_extracts_plugin_files_and_preserves_manifest_metadata(self):
        package = deployment.PackageVersion(
            "Creator",
            "Example",
            "1.0.0",
            "Creator-Example-1.0.0",
            "https://thunderstore.io/package/download/Creator/Example/1.0.0/",
            (),
        )
        payload = self.make_archive(
            {
                "manifest.json": b'{"name":"Example","version_number":"1.0.0"}',
                "README.md": b"readme",
                "icon.png": b"icon",
                "Example.dll": b"assembly",
                "BepInEx/config/example.cfg": b"config",
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            files = deployment.extract_plugin_files(package, payload, directory)
            self.assertEqual(
                files,
                {
                    "Creator-Example/Example.dll": b"assembly",
                    "Creator-Example/manifest.json": (
                        b'{"name":"Example","version_number":"1.0.0"}'
                    ),
                },
            )
            self.assertEqual(
                (
                    Path(directory)
                    / "Creator-Example"
                    / "Example.dll"
                ).read_bytes(),
                b"assembly",
            )
            self.assertEqual(
                (
                    Path(directory)
                    / "Creator-Example"
                    / "manifest.json"
                ).read_bytes(),
                b'{"name":"Example","version_number":"1.0.0"}',
            )

    def test_rejects_archive_path_traversal(self):
        package = deployment.PackageVersion(
            "Creator",
            "Example",
            "1.0.0",
            "Creator-Example-1.0.0",
            "https://thunderstore.io/package/download/Creator/Example/1.0.0/",
            (),
        )
        payload = self.make_archive(
            {"BepInEx/plugins/../escape.dll": b"unsafe"}
        )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "unsafe path"):
                deployment.extract_plugin_files(package, payload, directory)

    def test_normalizes_windows_separators_in_archive_paths(self):
        package = deployment.PackageVersion(
            "ValheimModding",
            "Jotunn",
            "2.30.2",
            "ValheimModding-Jotunn-2.30.2",
            "https://thunderstore.io/package/download/ValheimModding/Jotunn/2.30.2/",
            (),
        )
        payload = self.make_archive(
            {"plugins\\Jotunn.dll": b"assembly"}
        )

        with tempfile.TemporaryDirectory() as directory:
            files = deployment.extract_plugin_files(package, payload, directory)

        self.assertEqual(
            files,
            {"ValheimModding-Jotunn/plugins/Jotunn.dll": b"assembly"},
        )

    def test_rejects_symlink_payloads(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            item = zipfile.ZipInfo("BepInEx/plugins/escape.dll")
            item.create_system = 3
            item.external_attr = (0o120777 << 16)
            archive.writestr(item, b"../../outside")
        package = deployment.PackageVersion(
            "Creator",
            "Example",
            "1.0.0",
            "Creator-Example-1.0.0",
            "https://thunderstore.io/package/download/Creator/Example/1.0.0/",
            (),
        )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "contains a symlink"):
                deployment.extract_plugin_files(
                    package,
                    output.getvalue(),
                    directory,
                )

    def test_skips_bepinex_bootstrap_files_outside_plugins(self):
        package = deployment.PackageVersion(
            "denikson",
            "BepInExPack_Valheim",
            "5.4.2351",
            "denikson-BepInExPack_Valheim-5.4.2351",
            "https://thunderstore.io/package/download/denikson/BepInExPack_Valheim/5.4.2351/",
            (),
        )
        payload = self.make_archive(
            {
                "BepInExPack_Valheim/BepInEx/core/BepInEx.dll": b"loader",
                "BepInExPack_Valheim/start_server_bepinex.sh": b"launcher",
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            files = deployment.extract_plugin_files(package, payload, directory)

        self.assertEqual(files, {})

    def test_remote_path_must_be_absolute_and_normalized(self):
        self.assertEqual(
            deployment._validate_remote_path(
                "/server/BepInEx/plugins",
                "SFTP_REMOTE_PATH",
            ),
            "/server/BepInEx/plugins",
        )
        with self.assertRaisesRegex(ValueError, "normalized absolute"):
            deployment._validate_remote_path(
                "/server/../plugins",
                "SFTP_REMOTE_PATH",
            )
        with self.assertRaisesRegex(ValueError, "normalized absolute"):
            deployment._validate_remote_path("/", "SFTP_REMOTE_PATH")


class SftpPromotionTests(unittest.TestCase):
    class WritableRemoteFile(io.BytesIO):
        def __init__(self, sftp, path):
            super().__init__()
            self.sftp = sftp
            self.path = path

        def close(self):
            self.sftp.files[self.path] = self.getvalue()
            super().close()

    class FakeSftp:
        def __init__(self):
            self.directories = {"/", "/server", "/server/BepInEx"}
            self.files = {}

        def stat(self, path):
            if path in self.directories:
                return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755)
            if path in self.files:
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o644)
            raise FileNotFoundError(2, "not found", path)

        def lstat(self, path):
            return self.stat(path)

        def mkdir(self, path):
            self.directories.add(path)

        def put(self, local_path, remote_path):
            self.files[remote_path] = Path(local_path).read_bytes()

        def open(self, path, mode):
            if "r" in mode:
                if path not in self.files:
                    raise FileNotFoundError(2, "not found", path)
                return io.BytesIO(self.files[path])
            return SftpPromotionTests.WritableRemoteFile(self, path)

        def rename(self, source, target):
            if source in self.files:
                self.files[target] = self.files.pop(source)
                return
            if source not in self.directories:
                raise FileNotFoundError(2, "not found", source)
            source_prefix = source.rstrip("/") + "/"
            moved_directories = {
                path: (
                    target
                    if path == source
                    else posixpath.join(
                        target,
                        path[len(source) :].lstrip("/"),
                    )
                )
                for path in self.directories
                if path == source or path.startswith(source_prefix)
            }
            moved_files = {
                path: posixpath.join(
                    target,
                    path[len(source) :].lstrip("/"),
                )
                for path in self.files
                if path.startswith(source_prefix)
            }
            self.directories.difference_update(moved_directories)
            self.directories.update(moved_directories.values())
            for old_path, new_path in moved_files.items():
                self.files[new_path] = self.files.pop(old_path)

        def listdir_attr(self, path):
            prefix = path.rstrip("/") + "/"
            items = []
            for directory in self.directories:
                if directory.startswith(prefix):
                    remainder = directory[len(prefix) :]
                    if remainder and "/" not in remainder:
                        items.append(
                            SimpleNamespace(
                                filename=remainder,
                                st_mode=stat.S_IFDIR | 0o755,
                            )
                        )
            for filename in self.files:
                if filename.startswith(prefix):
                    remainder = filename[len(prefix) :]
                    if remainder and "/" not in remainder:
                        items.append(
                            SimpleNamespace(
                                filename=remainder,
                                st_mode=stat.S_IFREG | 0o644,
                            )
                        )
            return items

        def remove(self, path):
            del self.files[path]

        def rmdir(self, path):
            self.directories.remove(path)

        def close(self):
            pass

    class FakeSsh:
        def close(self):
            pass

    def test_upload_stages_files_then_promotes_them_to_plugins(self):
        sftp = self.FakeSftp()
        with tempfile.TemporaryDirectory() as directory:
            local_root = Path(directory)
            package_directory = local_root / "Example"
            package_directory.mkdir()
            package_file = package_directory / "plugin.dll"
            package_file.write_bytes(b"assembly")
            package_manifest = package_directory / "manifest.json"
            package_manifest.write_bytes(b'{"version_number":"1.0.0"}')
            package_files = (
                "Example/manifest.json",
                "Example/plugin.dll",
            )
            with mock.patch.object(
                deployment,
                "_connect_sftp",
                return_value=(self.FakeSsh(), sftp),
            ):
                staged = deployment.stage_plugin_files(
                    local_root,
                    package_files,
                    "server.example",
                    8822,
                    "user",
                    "password",
                    "[server.example]:8822 ssh-ed25519 key",
                    "/server/BepInEx/plugins",
                )

                self.assertEqual(
                    sftp.files[
                        staged.remote_stage_path + "/Example/plugin.dll"
                    ],
                    b"assembly",
                )
                self.assertEqual(
                    sftp.files[
                        staged.remote_stage_path + "/Example/manifest.json"
                    ],
                    b'{"version_number":"1.0.0"}',
                )
                self.assertNotIn(
                    "/server/BepInEx/plugins/Example/plugin.dll",
                    sftp.files,
                )

                deployment.promote_staged_deployment(
                    staged,
                    "server.example",
                    8822,
                    "user",
                    "password",
                    "[server.example]:8822 ssh-ed25519 key",
                    "/server/BepInEx/plugins",
                )

            self.assertEqual(
                sftp.files["/server/BepInEx/plugins/Example/plugin.dll"],
                b"assembly",
            )
            self.assertEqual(
                sftp.files["/server/BepInEx/plugins/Example/manifest.json"],
                b'{"version_number":"1.0.0"}',
            )
            with mock.patch.object(
                deployment,
                "_connect_sftp",
                return_value=(self.FakeSsh(), sftp),
            ):
                unchanged = deployment.stage_plugin_files(
                    local_root,
                    package_files,
                    "server.example",
                    8822,
                    "user",
                    "password",
                    "[server.example]:8822 ssh-ed25519 key",
                    "/server/BepInEx/plugins",
                )
                self.assertEqual(unchanged.staged_files, ())
                deployment.promote_staged_deployment(
                    unchanged,
                    "server.example",
                    8822,
                    "user",
                    "password",
                    "[server.example]:8822 ssh-ed25519 key",
                    "/server/BepInEx/plugins",
                )

            package_file.write_bytes(b"updated assembly")
            package_manifest.write_bytes(b'{"version_number":"1.1.0"}')
            with mock.patch.object(
                deployment,
                "_connect_sftp",
                return_value=(self.FakeSsh(), sftp),
            ):
                changed = deployment.stage_plugin_files(
                    local_root,
                    package_files,
                    "server.example",
                    8822,
                    "user",
                    "password",
                    "[server.example]:8822 ssh-ed25519 key",
                    "/server/BepInEx/plugins",
                )
                self.assertEqual(
                    changed.staged_files,
                    package_files,
                )
                deployment.promote_staged_deployment(
                    changed,
                    "server.example",
                    8822,
                    "user",
                    "password",
                    "[server.example]:8822 ssh-ed25519 key",
                    "/server/BepInEx/plugins",
                )
            self.assertEqual(
                sftp.files["/server/BepInEx/plugins/Example/plugin.dll"],
                b"updated assembly",
            )
            self.assertEqual(
                sftp.files["/server/BepInEx/plugins/Example/manifest.json"],
                b'{"version_number":"1.1.0"}',
            )

            sftp.files["/server/BepInEx/plugins/manual.dll"] = b"keep"
            sftp.files["/server/BepInEx/plugins/Example/manual.cfg"] = b"old"
            updated_root = local_root / "updated"
            updated_file = updated_root / "Example" / "new.dll"
            updated_file.parent.mkdir(parents=True)
            updated_file.write_bytes(b"new assembly")
            (updated_file.parent / "manifest.json").write_bytes(
                b'{"version_number":"1.2.0"}'
            )
            with mock.patch.object(
                deployment,
                "_connect_sftp",
                return_value=(self.FakeSsh(), sftp),
            ):
                updated = deployment.stage_plugin_files(
                    updated_root,
                    ("Example/manifest.json", "Example/new.dll"),
                    "server.example",
                    8822,
                    "user",
                    "password",
                    "[server.example]:8822 ssh-ed25519 key",
                    "/server/BepInEx/plugins",
                )
                self.assertEqual(
                    updated.replace_mods,
                    ("Example",),
                )
                deployment.promote_staged_deployment(
                    updated,
                    "server.example",
                    8822,
                    "user",
                    "password",
                    "[server.example]:8822 ssh-ed25519 key",
                    "/server/BepInEx/plugins",
                )

        self.assertNotIn(
            "/server/BepInEx/plugins/Example/plugin.dll",
            sftp.files,
        )
        self.assertEqual(
            sftp.files["/server/BepInEx/plugins/Example/new.dll"],
            b"new assembly",
        )
        self.assertEqual(
            sftp.files["/server/BepInEx/plugins/manual.dll"],
            b"keep",
        )
        self.assertNotIn(
            "/server/BepInEx/plugins/Example/manual.cfg",
            sftp.files,
        )
        self.assertFalse(
            any("starfinders-staging" in path for path in sftp.directories)
        )

    def test_sftp_requires_a_pinned_host_key_before_connecting(self):
        key = deployment.paramiko.RSAKey.generate(bits=1024)

        class FakeSsh:
            def __init__(self):
                self.host_keys = deployment.paramiko.HostKeys()
                self.policy = None
                self.connect = mock.Mock()
                self.open_sftp = mock.Mock(return_value=object())

            def load_host_keys(self, path):
                self.host_keys.load(path)

            def get_host_keys(self):
                return self.host_keys

            def set_missing_host_key_policy(self, policy):
                self.policy = policy

            def close(self):
                pass

        ssh = FakeSsh()
        known_hosts = (
            f"[server.example]:8822 {key.get_name()} {key.get_base64()}"
        )
        with mock.patch.object(
            deployment.paramiko,
            "SSHClient",
            return_value=ssh,
        ):
            _, sftp = deployment._connect_sftp(
                "server.example",
                8822,
                "user",
                "secret",
                known_hosts,
            )

        self.assertIsInstance(ssh.policy, deployment.paramiko.RejectPolicy)
        ssh.connect.assert_called_once()
        self.assertEqual(ssh.connect.call_args.kwargs["password"], "secret")
        self.assertIsNotNone(sftp)

    def test_sftp_rejects_an_unpinned_server_key(self):
        class FakeSsh:
            def __init__(self):
                self.host_keys = deployment.paramiko.HostKeys()
                self.connect = mock.Mock()

            def load_host_keys(self, path):
                self.host_keys.load(path)

            def get_host_keys(self):
                return self.host_keys

            def close(self):
                pass

        key = deployment.paramiko.RSAKey.generate(bits=1024)
        ssh = FakeSsh()
        known_hosts = (
            f"[other.example]:8822 {key.get_name()} {key.get_base64()}"
        )
        with (
            mock.patch.object(
                deployment.paramiko,
                "SSHClient",
                return_value=ssh,
            ),
            self.assertRaisesRegex(ValueError, "no verified key"),
        ):
            deployment._connect_sftp(
                "server.example",
                8822,
                "user",
                "secret",
                known_hosts,
            )

        ssh.connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
