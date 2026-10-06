import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from git_sync import GitWorkspace, SyncError


class GitTransportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="research-transport-")
        self.workspace = GitWorkspace(Path(self.temp.name) / "repo", Path(self.temp.name) / "data")
        self.operations = []

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, results, remote="https://github.com/example/private.git"):
        outcomes = iter(results)
        def run(*args, **kwargs):
            if args[:3] == ("remote", "get-url", "origin"):
                return subprocess.CompletedProcess(args, 0, stdout=remote, stderr="")
            if args[:3] == ("config", "--get-urlmatch", "http.proxy"):
                return subprocess.CompletedProcess(args, 0, stdout="socks5h://127.0.0.1:7890\n", stderr="")
            self.operations.append((args, kwargs))
            result = next(outcomes)
            if isinstance(result, Exception):
                raise result
            return subprocess.CompletedProcess(args, 0, stdout="success", stderr="")
        return run

    def test_transient_handshake_retries_without_force_or_security_changes(self):
        error = SyncError("gnutls_handshake() failed: Error in the pull function")
        with patch.dict(os.environ, {}, clear=True), patch.object(self.workspace, "git", side_effect=self.runner([error, True])):
            self.workspace.network_git("push", "origin", "HEAD:refs/heads/main")
        self.assertEqual(len(self.operations), 2)
        for args, kwargs in self.operations:
            self.assertNotIn("--force", args)
            self.assertFalse(any("sslVerify" in value for value in args))
            self.assertLessEqual(kwargs["timeout"], 45)

    def test_uses_existing_http_proxy_after_default_route_fails(self):
        error = SyncError("gnutls_handshake() failed")
        environment = {"https_proxy": "http://127.0.0.1:7890", "HTTPS_PROXY": "http://127.0.0.1:7890",
                       "ALL_PROXY": "socks://127.0.0.1:7890"}
        with patch.dict(os.environ, environment, clear=True), patch.object(self.workspace, "git", side_effect=self.runner([error, error, True, True])):
            self.workspace.network_git("fetch", "origin")
            self.workspace.network_git("push", "origin", "HEAD:refs/heads/main")
        option = "http.https://github.com.proxy=http://127.0.0.1:7890"
        self.assertIn(option, self.operations[2][0])
        self.assertIn(option, self.operations[3][0])
        self.assertEqual(self.workspace.network_proxy, "http://127.0.0.1:7890")

    def test_auth_non_fast_forward_and_certificate_errors_do_not_retry(self):
        for message in ["Authentication failed", "non-fast-forward", "gnutls_handshake: server certificate verification failed"]:
            self.operations.clear()
            with patch.dict(os.environ, {}, clear=True), patch.object(self.workspace, "git", side_effect=self.runner([SyncError(message)])):
                with self.assertRaisesRegex(SyncError, message):
                    self.workspace.network_git("fetch", "origin")
            self.assertEqual(len(self.operations), 1)

    def test_exhaustion_preserves_credentials_and_redacts_proxy_details(self):
        error = SyncError("connection reset proxy password SHOULD_NOT_LEAK")
        with patch.dict(os.environ, {}, clear=True), patch.object(self.workspace, "git", side_effect=self.runner([error, error])):
            with self.assertRaises(SyncError) as raised:
                self.workspace.network_git("fetch", "origin")
        self.assertIn("本机资料和提交均已保留", str(raised.exception))
        self.assertIn("不需要重新登录", str(raised.exception))
        self.assertNotIn("SHOULD_NOT_LEAK", str(raised.exception))

    def test_local_and_ssh_remotes_are_unchanged(self):
        for remote in ["/tmp/cloud.git", "git@github.com:example/private.git"]:
            self.operations.clear()
            with patch.object(self.workspace, "git", side_effect=self.runner([True], remote=remote)):
                self.workspace.network_git("fetch", "origin")
            self.assertEqual(self.operations[0][0], ("fetch", "origin"))

    def test_overall_retry_deadline_is_bounded(self):
        with patch.dict(os.environ, {}, clear=True), patch("git_sync.time.monotonic", side_effect=[0, 0, 91]), \
             patch.object(self.workspace, "git", side_effect=self.runner([SyncError("连接 GitHub 超时")])):
            with self.assertRaises(SyncError):
                self.workspace.network_git("fetch", "origin")
        self.assertEqual(len(self.operations), 1)

    def test_malformed_environment_proxy_does_not_crash_sync(self):
        with patch.dict(os.environ, {"https_proxy": "http://[malformed"}, clear=True), \
             patch.object(self.workspace, "git", side_effect=self.runner([True])):
            self.workspace.network_git("fetch", "origin")
        self.assertEqual(len(self.operations), 1)
