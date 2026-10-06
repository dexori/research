"""Portable Markdown records and conservative Git synchronization.

SQLite is a local projection; credentials and sessions never enter workspace/.
The remote operation is explicit and never force-pushes or resets the checkout.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit


class SyncError(Exception):
    pass


UUID = re.compile(r"[a-f0-9]{32}\Z")
GITHUB_URL = re.compile(r"(?:https://github\.com/|git@github\.com:)[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+(?:\.git)?\Z")
KINDS = {"project", "paper", "experiment", "dataset", "idea", "writing", "knowledge"}
STATUSES = {"待开始", "进行中", "已完成", "已归档"}
FIELDS = ["id", "kind", "title", "status", "tags", "project_id", "created_at", "updated_at"]


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".research-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def encode(record):
    metadata = {key: record[key] for key in FIELDS}
    metadata.update(schema=1, metadata=record["metadata"], attachments=record.get("attachments", []))
    return ("---\n" + json.dumps(metadata, ensure_ascii=False, indent=2) + "\n---\n\n" + record["content"]).encode("utf-8")


def decode(raw):
    try:
        text = raw.decode("utf-8").replace("\r\n", "\n")
        if not text.startswith("---\n") or len(raw) > 4 * 1024 * 1024:
            raise ValueError()
        header, body = text[4:].split("\n---\n", 1)
        record = json.loads(header)
        if record.get("schema") != 1:
            raise ValueError()
        if not UUID.fullmatch(record["id"]) or record["kind"] not in KINDS or record["status"] not in STATUSES:
            raise ValueError()
        for key in ["title", "tags", "created_at", "updated_at"]:
            if not isinstance(record[key], str):
                raise ValueError()
        if not record["title"].strip() or len(record["title"]) > 240:
            raise ValueError()
        if record["project_id"] is not None and not UUID.fullmatch(record["project_id"]):
            raise ValueError()
        if record["kind"] == "project" and record["project_id"]:
            raise ValueError()
        if not isinstance(record["metadata"], dict) or not all(isinstance(value, str) for value in record["metadata"].values()):
            raise ValueError()
        for key in ["url", "external_url"]:
            value = record["metadata"].get(key, "")
            if value and (urlsplit(value).scheme not in {"http", "https"} or not urlsplit(value).netloc):
                raise ValueError()
        if not isinstance(record.get("attachments", []), list):
            raise ValueError()
        seen = set()
        for file in record.get("attachments", []):
            if not UUID.fullmatch(file["id"]) or not UUID.fullmatch(file["storage_name"]):
                raise ValueError()
            if file["id"] in seen:
                raise ValueError()
            seen.add(file["id"])
            name = file["name"]
            if not isinstance(name, str) or name in {".", "..", ""} or "/" in name or "\\" in name or any(ord(c) < 32 for c in name):
                raise ValueError()
            if not isinstance(file["size"], int) or not 0 <= file["size"] <= 64 * 1024 * 1024:
                raise ValueError()
            if not isinstance(file["created_at"], str) or not re.fullmatch(r"[a-f0-9]{64}", file["sha256"]):
                raise ValueError()
        record["content"] = body[1:] if body.startswith("\n") else body
        return record
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError) as error:
        raise SyncError("资料文件格式无效，请在 GitHub 同步页面检查冲突或从 Git 历史恢复文件。") from error


class GitWorkspace:
    def __init__(self, repo, data_dir):
        self.repo = Path(repo).resolve()
        self.root = self.repo / "workspace"
        self.records = self.root / "records"
        self.records.mkdir(parents=True, exist_ok=True)
        self.data = Path(data_dir)
        self.lock = threading.RLock()
        self.stamps = {}
        self.error = None

    def git(self, *args, check=True, timeout=45):
        environment = os.environ.copy()
        environment.update(GIT_TERMINAL_PROMPT="0", GIT_MERGE_AUTOEDIT="no",
                           GIT_SSH_COMMAND="ssh -o BatchMode=yes -o ConnectTimeout=10")
        try:
            result = subprocess.run(["git", "-c", "core.quotePath=false", "-c", "commit.gpgsign=false",
                                     "-C", str(self.repo), *args], capture_output=True,
                                    encoding="utf-8", errors="replace", env=environment, timeout=timeout)
        except FileNotFoundError as error:
            raise SyncError("请先在这台电脑安装 Git。") from error
        except subprocess.TimeoutExpired as error:
            raise SyncError("连接 GitHub 超时。本机资料已保留，可稍后重试。") from error
        if check and result.returncode:
            output = (result.stderr or result.stdout).strip()
            if "Permission denied" in output or "Authentication failed" in output or "could not read Username" in output:
                raise SyncError("GitHub 登录尚未配置。请在此页面点击「登录 GitHub」授权，并使用 HTTPS 仓库地址；SSH 地址需要另行配置密钥。")
            raise SyncError("Git 操作未完成，本机资料已保留。" + ("\n" + output[:1500] if output else ""))
        return result

    def has_repo(self):
        return (self.repo / ".git").exists()

    def network_git(self, *args, timeout=45):
        """Retry only transient HTTPS transport errors, never auth/merge errors.

        Routes come only from existing configuration/environment; no proxy is
        invented, certificate verification is unchanged, and direct connections
        are not used to bypass a configured proxy. Fetch/push retries are safe:
        neither operation resets user data or force-pushes a branch.
        """
        remote = self.git("remote", "get-url", "origin", check=False).stdout.strip()
        if not remote.startswith("https://github.com/"):
            return self.git(*args, timeout=timeout)
        configured = self.git("config", "--get-urlmatch", "http.proxy", remote, check=False).stdout.strip()
        preferred = getattr(self, "network_proxy", None)
        routes = [preferred, preferred]
        seen = {configured, preferred}
        for name in ["https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"]:
            proxy = os.environ.get(name, "").strip()
            if proxy.startswith("socks://"):
                proxy = "socks5h://" + proxy[len("socks://"):]
            try:
                parsed = urlsplit(proxy)
                usable = parsed.scheme in {"http", "https", "socks5", "socks5h"} and bool(parsed.hostname)
            except ValueError:
                usable = False
            if not usable or proxy in seen:
                continue
            seen.add(proxy)
            routes.append(proxy)
        routes = routes[:4]
        deadline = time.monotonic() + 90
        for proxy in routes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            options = ["-c", "http.version=HTTP/1.1"]
            if proxy is not None:
                options += ["-c", "http.https://github.com.proxy=" + proxy]
            try:
                result = self.git(*options, *args, timeout=min(timeout, remaining))
                self.network_proxy = proxy
                return result
            except SyncError as error:
                reason = str(error).lower()
                transient = ["gnutls_handshake", "connection reset", "recv failure", "error in the pull function",
                             "failed to connect", "could not resolve host", "could not resolve proxy",
                             "tls connection was non-properly terminated", "empty reply from server",
                             "the requested url returned error: 502", "the requested url returned error: 503",
                             "the requested url returned error: 504", "连接 github 超时"]
                # Even when mixed with a handshake message, certificate failures
                # require diagnosis, not retries or reduced security.
                if any(word in reason for word in ["certificate", "ssl peer", "证书"]) or not any(word in reason for word in transient):
                    raise
        raise SyncError("连接 GitHub 时网络中断，自动重试后仍未连接成功。本机资料和提交均已保留，不需要重新登录。请检查代理或网络，再点击同步。")

    def conflicts(self):
        if not self.has_repo():
            return []
        result = self.git("diff", "--name-only", "--diff-filter=U", "-z")
        return [path for path in result.stdout.split("\0") if path]

    def is_merging(self):
        return self.has_repo() and self.git("rev-parse", "--verify", "MERGE_HEAD", check=False).returncode == 0

    def initialize(self):
        if not self.has_repo():
            self.git("init", "-b", "main")
            # Only the app surface is selected; runtime data and secrets are excluded.
            allowed = ["app.py", "git_sync.py", "github_auth.py", "codex_bridge.py", "draft_import.py", "research_cli.py", "math_compat.py", "paper_reader.py", "desktop_launcher.py", "learning.py", "requirements.txt", "README.md", ".gitignore", ".gitattributes",
                       "templates", "static", "tests", "research-hub.service.example", "workspace"]
            files = [path for path in allowed if (self.repo / path).exists()]
            if files:
                self.git("add", "--", *files)
                self.commit("Initialize private research workspace")
        top = self.git("rev-parse", "--show-toplevel").stdout.strip()
        if Path(top).resolve() != self.repo:
            raise SyncError("科研平台必须位于独立仓库中。")
        if self.git("config", "user.name", check=False).returncode:
            self.git("config", "user.name", "Research Hub")
        if self.git("config", "user.email", check=False).returncode:
            self.git("config", "user.email", "research-hub@localhost")
        branch = self.git("symbolic-ref", "--short", "HEAD", check=False)
        if branch.returncode:
            raise SyncError("当前仓库未处于分支上，请先切换到自己的工作分支。")

    def commit(self, message, paths=None):
        identity = []
        if self.git("config", "user.name", check=False).returncode or self.git("config", "user.email", check=False).returncode:
            identity = ["-c", "user.name=Research Hub", "-c", "user.email=research-hub@localhost"]
        command = [*identity, "commit", "-m", message]
        if paths:
            command.extend(["--only", "--", *paths])
        self.git(*command)

    def configure(self, remote, branch):
        if not GITHUB_URL.fullmatch(remote) or remote.endswith(("/.", "/..")):
            raise SyncError("请填写 GitHub 仓库地址，例如 https://github.com/用户名/research-hub.git。不要把访问令牌写入地址。")
        if not branch or branch.startswith("-") or self.git("check-ref-format", "--branch", branch, check=False).returncode:
            raise SyncError("分支名称无效，例如可以使用 main。")
        self.initialize()
        existing = self.git("remote", "get-url", "origin", check=False)
        if existing.returncode:
            self.git("remote", "add", "origin", remote)
        else:
            self.git("remote", "set-url", "origin", remote)
        self.git("config", "researchhub.branch", branch)

    def attachment_path(self, file):
        # The original name lives in metadata; the portable path also works on Windows.
        suffix = Path(file["name"]).suffix.lower()
        suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ".bin"
        path = self.root / "attachments" / file["storage_name"] / ("attachment" + suffix)
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise SyncError("附件路径无效。")
        return path

    def write_entry(self, connection, identifier):
        row = connection.execute("SELECT * FROM entries WHERE id=?", (identifier,)).fetchone()
        record = dict(row)
        record["metadata"] = json.loads(record["metadata"])
        files = []
        for attachment in connection.execute("SELECT * FROM attachments WHERE entry_id=? ORDER BY created_at,id", (identifier,)):
            file = {key: attachment[key] for key in ["id", "name", "storage_name", "size", "created_at"]}
            source = self.data / "uploads" / file["storage_name"]
            target = self.attachment_path(file)
            file["sha256"] = digest(source)
            if not target.exists() or digest(target) != file["sha256"]:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            files.append(file)
        record["attachments"] = files
        raw = encode(record)
        if len(raw) > 4 * 1024 * 1024:
            raise SyncError("单条笔记及其元信息超过 4 MB，请拆分记录。")
        path = self.records / f"{identifier}.md"
        atomic_write(path, raw)
        connection.execute("INSERT OR REPLACE INTO sync_files VALUES (?,?)", (identifier, hashlib.sha256(raw).hexdigest()))

    def refresh(self, connection):
        # Conflicted files are intentionally left untouched until the user resolves them.
        if self.conflicts():
            return
        pending = []
        for path in sorted(self.records.glob("*.md")):
            if path.is_symlink() or not UUID.fullmatch(path.stem):
                raise SyncError("资料目录含有无法识别的文件名。请保留平台生成的记录 ID。")
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if self.stamps.get(path) == stamp:
                continue
            raw = path.read_bytes()
            sha = hashlib.sha256(raw).hexdigest()
            baseline = connection.execute("SELECT hash FROM sync_files WHERE entry_id=?", (path.stem,)).fetchone()
            missing_local = any(not (self.data / "uploads" / row["storage_name"]).is_file()
                                for row in connection.execute("SELECT storage_name FROM attachments WHERE entry_id=?", (path.stem,)))
            if baseline and baseline[0] == sha and not missing_local:
                self.stamps[path] = stamp
                continue
            record = decode(raw)
            if record["id"] != path.stem:
                raise SyncError("资料文件名与记录 ID 不一致。")
            for file in record.get("attachments", []):
                source = self.attachment_path(file)
                if not source.is_file() or source.stat().st_size != file["size"] or digest(source) != file["sha256"]:
                    raise SyncError(f"附件未完整拉取：{file['name']}。请重新拉取仓库，本机已有资料仍保留。")
            pending.append((path, stamp, sha, record))
        if not pending:
            self.error = None
            return
        known = {row["id"]: row["kind"] for row in connection.execute("SELECT id,kind FROM entries")}
        known.update({record["id"]: record["kind"] for _, _, _, record in pending})
        attachment_owners = {row["id"]: row["entry_id"] for row in connection.execute("SELECT id,entry_id FROM attachments")}
        storage_owners = {row["storage_name"]: row["entry_id"] for row in connection.execute("SELECT storage_name,entry_id FROM attachments")}
        for _, _, _, record in pending:
            if record["project_id"] and known.get(record["project_id"]) != "project":
                raise SyncError("关联项目未完整拉取，请检查仓库中的项目记录。")
            for file in record.get("attachments", []):
                if attachment_owners.get(file["id"], record["id"]) != record["id"] or storage_owners.get(file["storage_name"], record["id"]) != record["id"]:
                    raise SyncError("不同记录的附件 ID 重复，请检查同步冲突。")
                attachment_owners[file["id"]] = record["id"]
                storage_owners[file["storage_name"]] = record["id"]
        try:
            connection.execute("PRAGMA defer_foreign_keys=ON")
            for path, stamp, sha, record in pending:
                values = [record[key] for key in FIELDS]
                values.extend([record["content"], json.dumps(record["metadata"], ensure_ascii=False)])
                connection.execute("""INSERT INTO entries (id,kind,title,status,tags,project_id,created_at,updated_at,content,metadata)
                    VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    kind=excluded.kind,title=excluded.title,status=excluded.status,tags=excluded.tags,
                    project_id=excluded.project_id,created_at=excluded.created_at,updated_at=excluded.updated_at,
                    content=excluded.content,metadata=excluded.metadata,version=entries.version+1""", values)
                connection.execute("DELETE FROM attachments WHERE entry_id=?", (record["id"],))
                for file in record.get("attachments", []):
                    source = self.attachment_path(file)
                    target = self.data / "uploads" / file["storage_name"]
                    if not target.exists() or digest(target) != file["sha256"]:
                        shutil.copyfile(source, target)
                    connection.execute("INSERT INTO attachments VALUES (?,?,?,?,?,?)", (file["id"], record["id"], file["name"], file["storage_name"], file["size"], file["created_at"]))
                connection.execute("INSERT OR REPLACE INTO sync_files VALUES (?,?)", (record["id"], sha))
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        for path, stamp, _, _ in pending:
            self.stamps[path] = stamp
        self.error = None

    def migrate(self, connection):
        self.refresh(connection)
        if self.conflicts() or self.error:
            return
        for row in connection.execute("SELECT id FROM entries").fetchall():
            if not (self.records / f"{row['id']}.md").exists():
                self.write_entry(connection, row["id"])
        connection.commit()

    def status(self):
        state = {"connected": False, "remote": "", "branch": "main", "dirty": 0,
                 "ahead": 0, "behind": 0, "conflicts": [], "merging": False,
                 "record_count": len(list(self.records.glob("*.md"))), "error": self.error}
        if not self.has_repo():
            state["dirty"] = sum(1 for path in self.root.rglob("*") if path.is_file())
            return state
        remote = self.git("remote", "get-url", "origin", check=False)
        if not remote.returncode:
            # Never display credential-bearing addresses originating from external git config.
            state["remote"] = remote.stdout.strip() if GITHUB_URL.fullmatch(remote.stdout.strip()) else "（请重新配置 GitHub 地址）"
            state["connected"] = True
        branch = self.git("config", "researchhub.branch", check=False)
        if branch.returncode:
            branch = self.git("symbolic-ref", "--short", "HEAD", check=False)
        state["branch"] = branch.stdout.strip() or "main"
        state["conflicts"] = self.conflicts()
        state["conflict_details"] = {}
        for path in state["conflicts"]:
            if re.fullmatch(r"workspace/records/[a-f0-9]{32}\.md", path):
                detail = {}
                for stage, label in [(2, "local"), (3, "remote")]:
                    blob = self.git("show", f":{stage}:{path}", check=False)
                    if not blob.returncode:
                        try:
                            record = decode(blob.stdout.encode("utf-8"))
                            detail[label] = {"title": record["title"], "content": record["content"][:1200]}
                        except SyncError:
                            pass
                state["conflict_details"][path] = detail
        state["merging"] = self.is_merging()
        state["dirty"] = len(self.git("status", "--porcelain", "--", "workspace").stdout.splitlines())
        counts = self.git("rev-list", "--left-right", "--count", f"HEAD...refs/remotes/origin/{state['branch']}", check=False)
        if not counts.returncode:
            state["ahead"], state["behind"] = map(int, counts.stdout.split())
        elif state["connected"]:
            total = self.git("rev-list", "--count", "HEAD", check=False)
            state["ahead"] = int(total.stdout.strip() or "0")
        return state

    def checkpoint(self):
        self.git("add", "--", "workspace")
        if self.git("diff", "--cached", "--quiet", "--", "workspace", check=False).returncode:
            self.commit(f"Research notes {datetime.now():%Y-%m-%d %H:%M:%S}", ["workspace"])

    def sync(self, connection):
        self.initialize()
        if self.conflicts():
            raise SyncError("存在同步冲突，请先处理下方的冲突记录。")
        if self.is_merging():
            self.commit("Resolve research workspace conflicts")
        self.refresh(connection)
        self.checkpoint()
        remote = self.git("remote", "get-url", "origin", check=False)
        if remote.returncode:
            raise SyncError("请先填写 GitHub 私有仓库地址。资料已在本机保存。")
        state = self.status()
        branch = state["branch"]
        # An ordinary merge retains both machines' commits and all local changes.
        self.network_git("fetch", "origin")
        ref = f"refs/remotes/origin/{branch}"
        if self.git("rev-parse", "--verify", ref, check=False).returncode == 0:
            staged = self.git("diff", "--cached", "--name-only").stdout.strip()
            if staged:
                raise SyncError("仓库中还有其他已暂存的代码修改，请先完成这些修改的提交，再同步资料。")
            merged = self.git("merge", "--no-edit", "--allow-unrelated-histories", ref, check=False)
            if merged.returncode:
                if self.conflicts():
                    raise SyncError("两台电脑修改了同一份资料。两个版本均已保留，请在下方选择处理方式。")
                raise SyncError("暂时无法合并 GitHub 的更新。本机资料与提交已保留，请检查仓库中的代码修改。")
        else:
            other = self.git("for-each-ref", "--format=%(refname)", "refs/remotes/origin").stdout.strip()
            if other:
                raise SyncError("GitHub 上没有所选分支，请填写仓库实际使用的分支名称。")
        self.stamps.clear()
        self.refresh(connection)
        self.network_git("push", "origin", f"HEAD:refs/heads/{branch}")
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        connection.execute("INSERT OR REPLACE INTO settings VALUES ('last_git_sync',?)", (stamp,))
        connection.commit()
        return stamp

    def resolve(self, path, choice):
        if path not in self.conflicts() or choice not in {"both", "local", "remote"}:
            raise SyncError("请选择有效的冲突记录和处理方式。")
        target = (self.repo / path).resolve()
        if not target.is_relative_to(self.repo) or target.is_relative_to(self.repo / ".git"):
            raise SyncError("冲突文件路径无效。")
        local = self.git("show", f":2:{path}", check=False)
        remote = self.git("show", f":3:{path}", check=False)
        if local.returncode or remote.returncode:
            raise SyncError("此冲突涉及文件删除或改名，请使用 Git 工具处理；平台不会自动丢弃任何版本。")
        local_bytes, remote_bytes = local.stdout.encode("utf-8"), remote.stdout.encode("utf-8")
        backup = self.data / "sync-conflicts" / uuid.uuid4().hex
        atomic_write(backup / "local.txt", local_bytes)
        atomic_write(backup / "remote.txt", remote_bytes)
        if choice == "both":
            if not re.fullmatch(r"workspace/records/[a-f0-9]{32}\.md", path):
                raise SyncError("代码文件请使用 Git 工具处理，或选择保留本机 / GitHub 版本。两份副本均已保存到本机。")
            record = decode(remote_bytes)
            record["id"] = uuid.uuid4().hex
            record["title"] = (record["title"][:230] + "（另一版本）")[:240]
            record["created_at"] = record["updated_at"] = datetime.now(timezone.utc).isoformat()
            for file in record.get("attachments", []):
                source = self.attachment_path(file)
                file["id"] = uuid.uuid4().hex
                file["storage_name"] = uuid.uuid4().hex
                destination = self.attachment_path(file)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
            atomic_write(self.records / f"{record['id']}.md", encode(record))
        atomic_write(target, remote_bytes if choice == "remote" else local_bytes)
        self.git("add", "--", path, "workspace")
        if not self.conflicts():
            self.commit("Resolve research workspace conflicts")
            self.stamps.clear()
