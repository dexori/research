"""Local GitHub CLI device authorization; never return credentials to the browser."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.request
import zipfile

from git_sync import atomic_write

DEVICE_URL = "https://github.com/login/device"
BUSY = {"checking", "preparing", "awaiting"}


class GitHubAuth:
    def __init__(self, workspace, auto_check=True):
        self.workspace = workspace
        self.auto_check = auto_check
        self.lock = threading.RLock()
        self.worker = None
        self.state = {"status": "signed_out", "account": "", "code": "",
                      "url": DEVICE_URL, "message": "在这台电脑登录 GitHub，完成一次授权即可。"}
        self.checked_at = 0

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def snapshot(self):
        with self.lock:
            if self.auto_check and not self.worker and not self.checked_at:
                self._launch(False)
            return dict(self.state)

    def start(self):
        with self.lock:
            if not self.worker or not self.worker.is_alive():
                self._launch(True)
            return dict(self.state)

    def _launch(self, login):
        self.update(status="preparing" if login else "checking", code="", account="",
                    message="正在准备登录…" if login else "正在检查本机 GitHub 登录…")
        self.worker = threading.Thread(target=self._run, args=(login,), daemon=True)
        self.worker.start()

    def find_cli(self):
        binary = shutil.which("gh")
        if binary:
            return Path(binary).resolve()
        executable = "gh.exe" if os.name == "nt" else "gh"
        candidates = sorted((self.workspace.repo / ".tools").glob(f"gh_*/bin/{executable}"))
        return candidates[-1].resolve() if candidates else None

    @staticmethod
    def download(url, limit):
        # Only official release/API URLs are accepted, never input from the web form.
        if not url.startswith(("https://api.github.com/repos/cli/cli/", "https://github.com/cli/cli/releases/")):
            raise ValueError("非官方下载地址")
        request = urllib.request.Request(url, headers={"User-Agent": "Research-Hub", "Accept": "application/vnd.github+json"})
        last_error = None
        for opener in [urllib.request.build_opener(), urllib.request.build_opener(urllib.request.ProxyHandler({}))]:
            try:
                with opener.open(request, timeout=30) as response:
                    data = response.read(limit + 1)
                if len(data) > limit:
                    raise ValueError("下载文件超出限制")
                return data
            except (OSError, ValueError) as error:
                last_error = error
        raise last_error

    def install_cli(self):
        system = {"Linux": "linux", "Darwin": "macOS", "Windows": "windows"}.get(platform.system())
        arch = {"x86_64": "amd64", "AMD64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine())
        if not system or not arch:
            raise ValueError("此系统请先自行安装 GitHub CLI，再点击登录。")
        self.update(message="首次使用：正在下载并校验官方 GitHub 登录工具，请稍候…")
        release = json.loads(self.download("https://api.github.com/repos/cli/cli/releases/latest", 2 * 1024 * 1024))
        suffix = f"_{system}_{arch}." + ("tar.gz" if system == "linux" else "zip")
        asset = next(item for item in release["assets"] if item["name"].endswith(suffix))
        checksum = next(item for item in release["assets"] if item["name"].endswith("_checksums.txt"))
        checksums = self.download(checksum["browser_download_url"], 1024 * 1024).decode("utf-8")
        expected = next(line.split()[0] for line in checksums.splitlines() if line.split()[-1].lstrip("*") == asset["name"])
        archive = self.download(asset["browser_download_url"], 64 * 1024 * 1024)
        if hashlib.sha256(archive).hexdigest() != expected:
            raise ValueError("官方登录工具校验失败，请重试。")
        # Extract ONLY the executable, never archive paths (including symlinks).
        executable = "gh.exe" if system == "windows" else "gh"
        if system == "linux":
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
                member = next(item for item in package.getmembers() if item.isfile() and item.name.endswith(f"/bin/{executable}"))
                if member.size > 128 * 1024 * 1024:
                    raise ValueError("登录工具大小异常")
                with package.extractfile(member) as stream:
                    payload = stream.read()
        else:
            with zipfile.ZipFile(io.BytesIO(archive)) as package:
                member = next(item for item in package.infolist() if item.filename.endswith(f"/bin/{executable}"))
                if member.file_size > 128 * 1024 * 1024:
                    raise ValueError("登录工具大小异常")
                payload = package.read(member)
        tag = release["tag_name"]
        if not re.fullmatch(r"v[0-9.]+", tag):
            raise ValueError("登录工具版本无效")
        target = self.workspace.repo / ".tools" / f"gh_{tag[1:]}_{system}_{arch}" / "bin" / executable
        atomic_write(target, payload)
        target.chmod(0o700)
        return target.resolve()

    def account(self, binary):
        result = subprocess.run([str(binary), "auth", "status", "--active", "--json", "hosts"],
                                capture_output=True, text=True, timeout=20)
        try:
            accounts = json.loads(result.stdout).get("hosts", {}).get("github.com", [])
            for row in accounts:
                if not row.get("active"):
                    continue
                if row.get("state") == "success":
                    return row["login"]
                reason = str(row.get("error", "")).lower()
                if any(value in reason for value in ["401", "invalid token", "bad credentials"]):
                    return ""
                # Saved credentials with a transport error are not a logged-out account.
                raise RuntimeError("暂时无法验证 GitHub 登录，请检查网络后重试。")
            return ""
        except (ValueError, KeyError, TypeError):
            raise RuntimeError("暂时无法验证 GitHub 登录，请检查网络后重试。") from None

    def connect_git(self, binary):
        # Repository-local helper only. Neither a token nor any global Git config is modified.
        helper = "!" + shlex.quote(str(binary).replace("\\", "/")) + " auth git-credential"
        with self.workspace.lock:
            self.workspace.initialize()
            self.workspace.git("config", "--local", "--replace-all", "credential.https://github.com.helper", "")
            self.workspace.git("config", "--local", "--add", "credential.https://github.com.helper", helper)

    def consume_output(self, process):
        for line in process.stdout:
            match = re.search(r"one-time code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})", line)
            if match:
                self.update(status="awaiting", code=match[1], message="复制下方授权码，在 GitHub 页面输入并确认授权。此页面会自动更新登录状态。")

    def device_login(self, binary):
        environment = os.environ.copy()
        environment.update(GH_BROWSER="echo", GH_PAGER="cat", NO_COLOR="1")
        process = subprocess.Popen([str(binary), "auth", "login", "--hostname", "github.com",
                                    "--git-protocol", "https", "--web", "--clipboard=false"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace", env=environment)
        reader = threading.Thread(target=self.consume_output, args=(process,), daemon=True)
        reader.start()
        try:
            # No terminal prompts or desktop browser are needed: the webpage shows the code/link.
            try:
                process.stdin.write("\n")
                process.stdin.flush()
            except BrokenPipeError:
                pass
            result = process.wait(timeout=15 * 60)
            reader.join(timeout=2)
            if result:
                raise RuntimeError("GitHub 授权未完成或连接中断。请检查网络，点击重新登录获取新授权码。")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=2)
            process.stdin.close()
            process.stdout.close()

    def _run(self, login):
        try:
            binary = self.find_cli()
            if not binary and not login:
                self.update(status="signed_out", message="这台电脑尚未登录，点击下方按钮开始授权。")
                return
            binary = binary or self.install_cli()
            account = self.account(binary)
            if not account and login:
                self.device_login(binary)
                account = self.account(binary)
                if not account:
                    raise RuntimeError("授权尚未生效，请稍后重试。")
            if account:
                self.connect_git(binary)
                self.update(status="signed_in", account=account, code="", message=f"已登录 GitHub：{account}。可以同步资料。")
            else:
                self.update(status="signed_out", code="", message="这台电脑尚未登录，点击下方按钮开始授权。")
        except subprocess.TimeoutExpired:
            self.update(status="error", code="", message="GitHub 连接超时或授权码已过期。资料未受影响，请检查网络后重新登录。")
        except Exception:
            # Never forward CLI output (potential credentials) to browser or logs.
            self.update(status="error", code="", message="GitHub 登录未完成。请检查网络并重试；也可先安装官方 GitHub CLI，再点击登录。")
        finally:
            with self.lock:
                self.checked_at = time.monotonic()
