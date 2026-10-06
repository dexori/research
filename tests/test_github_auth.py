import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zipfile

from app import create_app
from github_auth import GitHubAuth
from git_sync import GitWorkspace, atomic_write


class GitHubAuthTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="research-auth-test-")
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        atomic_write(self.repo / "app.py", b"# isolated app fixture\n")
        self.workspace = GitWorkspace(self.repo, Path(self.temp.name) / "data")
        self.auth = GitHubAuth(self.workspace, auto_check=False)

    def tearDown(self):
        self.temp.cleanup()

    def test_existing_login_reused_with_local_git_helper(self):
        binary = Path("/example/gh")
        with patch.object(self.auth, "find_cli", return_value=binary), \
             patch.object(self.auth, "account", return_value="researcher"), \
             patch.object(self.auth, "device_login") as login:
            self.auth._run(True)
        login.assert_not_called()
        self.assertEqual(self.auth.snapshot()["status"], "signed_in")
        helper = self.workspace.git("config", "--local", "--get-all", "credential.https://github.com.helper").stdout
        self.assertIn("/example/gh auth git-credential", helper)
        self.assertNotIn("token", json.dumps(self.auth.snapshot()))

    def test_device_code_and_success(self):
        binary = Path("/example/gh")
        with patch.object(self.auth, "find_cli", return_value=binary), \
             patch.object(self.auth, "account", side_effect=["", "researcher"]), \
             patch.object(self.auth, "connect_git") as connect:
            process = Mock()
            process.stdout = io.StringIO("! First copy your one-time code: ABCD-1234\nPress Enter to open https://github.com/login/device\n")
            process.stdin = io.StringIO()
            process.poll.return_value = 0
            def wait(**kwargs):
                # Confirm the code is visible while the subprocess is waiting.
                reader_seen.wait(2)
                self.assertEqual(self.auth.snapshot()["code"], "ABCD-1234")
                return 0
            reader_seen = threading.Event()
            original = self.auth.consume_output
            def consume(proc):
                original(proc)
                reader_seen.set()
            process.wait.side_effect = wait
            with patch("github_auth.subprocess.Popen", return_value=process) as launch, patch.object(self.auth, "consume_output", side_effect=consume):
                self.auth._run(True)
            arguments = launch.call_args.args[0]
            self.assertIn("--web", arguments)
            self.assertIn("https", arguments)
            self.assertNotIn("--with-token", arguments)
            connect.assert_called_once_with(binary)
        state = self.auth.snapshot()
        self.assertEqual(state["status"], "signed_in")
        self.assertEqual(state["code"], "")

    def test_no_tool_check_does_not_download(self):
        with patch.object(self.auth, "find_cli", return_value=None), patch.object(self.auth, "install_cli") as install:
            self.auth._run(False)
        install.assert_not_called()
        self.assertEqual(self.auth.snapshot()["status"], "signed_out")

    def test_background_start_is_nonblocking_and_duplicate_click_safe(self):
        entered, release = threading.Event(), threading.Event()
        def run(login):
            entered.set()
            release.wait(3)
        with patch.object(self.auth, "_run", side_effect=run) as worker:
            self.assertEqual(self.auth.start()["status"], "preparing")
            self.assertTrue(entered.wait(1))
            self.auth.start()
            worker.assert_called_once()
            release.set()
            self.auth.worker.join(2)

    def test_timeout_clears_code_and_does_not_leak_credentials(self):
        with patch.object(self.auth, "find_cli", return_value=Path("/fake/gh")), \
             patch.object(self.auth, "account", return_value=""), \
             patch.object(self.auth, "device_login", side_effect=subprocess.TimeoutExpired("ghp_DO_NOT_EXPOSE", 900)):
            self.auth.update(code="ABCD-1234")
            self.auth._run(True)
        state = self.auth.snapshot()
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["code"], "")
        self.assertNotIn("ghp_", json.dumps(state))

    def test_install_only_verified_binary_on_each_platform(self):
        payload = b"official-cli-fixture"
        for system, target_system, arch in [("Linux", "linux", "x86_64"), ("Darwin", "macOS", "arm64"), ("Windows", "windows", "AMD64")]:
            executable = "gh.exe" if system == "Windows" else "gh"
            stream = io.BytesIO()
            if system == "Linux":
                with tarfile.open(fileobj=stream, mode="w:gz") as package:
                    member = tarfile.TarInfo(f"root/bin/{executable}")
                    member.size = len(payload)
                    package.addfile(member, io.BytesIO(payload))
            else:
                with zipfile.ZipFile(stream, "w") as package:
                    package.writestr(f"root/bin/{executable}", payload)
                    package.writestr("../../evil.txt", "never extract")
            archive = stream.getvalue()
            archive_name = f"gh_1.2.3_{target_system}_{'arm64' if arch == 'arm64' else 'amd64'}." + ("tar.gz" if system == "Linux" else "zip")
            release = {"tag_name": "v1.2.3", "assets": [
                {"name": archive_name, "browser_download_url": "https://github.com/cli/cli/releases/download/v1.2.3/archive"},
                {"name": "gh_1.2.3_checksums.txt", "browser_download_url": "https://github.com/cli/cli/releases/download/v1.2.3/checksums"}]}
            checksum = (hashlib.sha256(archive).hexdigest() + "  " + archive_name).encode()
            with patch("github_auth.platform.system", return_value=system), patch("github_auth.platform.machine", return_value=arch), \
                 patch.object(self.auth, "download", side_effect=[json.dumps(release).encode(), checksum, archive]):
                binary = self.auth.install_cli()
            self.assertEqual(binary.read_bytes(), payload)
            self.assertFalse((Path(self.temp.name) / "evil.txt").exists())
            with patch("github_auth.platform.system", return_value=system), patch("github_auth.platform.machine", return_value=arch), \
                 patch.object(self.auth, "download", side_effect=[json.dumps(release).encode(), checksum, b"tampered"]):
                with self.assertRaises(ValueError):
                    self.auth.install_cli()

    def test_auth_endpoints_require_owner_login_and_csrf(self):
        app = create_app(Path(self.temp.name) / "app-data", testing=True)
        client = app.test_client()
        self.assertEqual(client.get("/sync/github/status").location, "/setup")
        client.get("/setup")
        with client.session_transaction() as session:
            csrf = session["csrf"]
        client.post("/setup", data={"csrf": csrf, "password": "test-password-2026", "confirm": "test-password-2026"})
        fake = Mock()
        fake.snapshot.return_value = {"status": "signed_out", "code": "", "message": "尚未登录"}
        fake.start.return_value = {"status": "awaiting", "code": "ABCD-1234", "message": "请授权"}
        app.extensions["github_auth"] = fake
        self.assertEqual(client.post("/sync/github/login").status_code, 400)
        self.assertEqual(client.get("/sync/github/status").json["status"], "signed_out")
        with client.session_transaction() as session:
            csrf = session["csrf"]
        response = client.post("/sync/github/login", data={"csrf": csrf})
        self.assertEqual(response.json["code"], "ABCD-1234")
        self.assertIn("这台电脑的 GitHub 登录".encode(), client.get("/sync").data)
        self.assertEqual(app.test_client().get("/sync/github/status").location, "/login")


if __name__ == "__main__":
    unittest.main()
