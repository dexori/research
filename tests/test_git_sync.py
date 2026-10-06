import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing

from app import create_app, ROOT
from git_sync import decode, encode, SyncError


class GitSyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="research-git-test-")
        self.root = Path(self.temp.name)
        self.remote = self.root / "cloud.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.remote)], check=True, capture_output=True)
        self.repo_a = self.root / "computer-a"
        self.repo_a.mkdir()
        for file in ["app.py", "git_sync.py", "github_auth.py", "codex_bridge.py", ".gitignore", ".gitattributes", "requirements.txt", "README.md"]:
            shutil.copyfile(ROOT / file, self.repo_a / file)
        self.a = create_app(self.repo_a / "data", testing=True, repo_dir=self.repo_a)
        self.client_a = self.setup_client(self.a)
        self.ws_a = self.a.extensions["git_workspace"]
        self.ws_a.initialize()
        self.ws_a.git("remote", "add", "origin", str(self.remote))

    def tearDown(self):
        self.temp.cleanup()

    def post(self, client, route, **data):
        client.get("/")
        with client.session_transaction() as session:
            token = session["csrf"]
        return client.post(route, data={"csrf": token, **data})

    def setup_client(self, app):
        client = app.test_client()
        client.get("/setup")
        response = self.post(client, "/setup", password="isolated-password-2026", confirm="isolated-password-2026")
        self.assertEqual(response.status_code, 302)
        return client

    def create(self, client, kind="paper", **data):
        values = {"title": "科研笔记", "status": "进行中", "content": "## 阅读\n\n原始想法", **data}
        response = self.post(client, f"/new/{kind}", **values)
        self.assertEqual(response.status_code, 302, response.data)
        return response.location.split("/")[-1]

    def connection(self, app):
        db = sqlite3.connect(app.config["DATA_DIR"] / "research.sqlite3")
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return closing(db)

    def sync(self, app):
        with self.connection(app) as db:
            return app.extensions["git_workspace"].sync(db)

    def clone_b(self):
        self.repo_b = self.root / "computer-b"
        subprocess.run(["git", "clone", str(self.remote), str(self.repo_b)], check=True, capture_output=True)
        self.b = create_app(self.repo_b / "data", testing=True, repo_dir=self.repo_b)
        self.client_b = self.setup_client(self.b)
        self.ws_b = self.b.extensions["git_workspace"]

    def test_zip_download_can_initialize_and_merge_remote_history(self):
        identifier = self.create(self.client_a)
        self.sync(self.a)
        repo = self.root / "computer-from-zip"
        shutil.copytree(self.repo_a, repo, ignore=shutil.ignore_patterns(".git", "data", ".tools"))
        app = create_app(repo / "data", testing=True, repo_dir=repo)
        workspace = app.extensions["git_workspace"]
        workspace.initialize()
        workspace.git("remote", "add", "origin", str(self.remote))
        client = self.setup_client(app)
        self.assertEqual(client.get(f"/entry/{identifier}").status_code, 200)
        self.sync(app)
        self.assertEqual(workspace.conflicts(), [])
        self.assertEqual(workspace.status()["ahead"], 0)
        self.assertTrue((repo / "github_auth.py").exists())

    def test_new_computer_restores_projects_records_and_attachments_without_original(self):
        project = self.create(self.client_a, "project", title="研究项目")
        paper = self.create(self.client_a, project_id=project, title="文献笔记")
        self.create(self.client_a, "experiment", project_id=project, title="实验", model="SmolVLA", metric_name="SR (%)", metric_value="76")
        payload = b"%PDF-1.4 saved-paper"
        self.post(self.client_a, f"/entry/{paper}/upload", files=(io.BytesIO(payload), "论文.pdf"))
        self.sync(self.a)
        tracked = self.ws_a.git("ls-tree", "-r", "--name-only", "HEAD").stdout
        self.assertIn("workspace/records/", tracked)
        self.assertIn("workspace/attachments/", tracked)
        self.assertNotIn("session.key", tracked)
        self.assertNotIn("research.sqlite3", tracked)
        self.assertFalse(any(path.startswith("data/") for path in tracked.splitlines()))
        self.clone_b()
        page = self.client_b.get(f"/entry/{paper}")
        self.assertIn("文献笔记".encode(), page.data)
        self.assertIn("研究项目".encode(), page.data)
        with self.connection(self.b) as db:
            file = db.execute("SELECT * FROM attachments WHERE entry_id=?", (paper,)).fetchone()
            self.assertEqual(db.execute("SELECT count(*) FROM entries").fetchone()[0], 3)
        self.assertEqual((self.b.config["DATA_DIR"] / "uploads" / file["storage_name"]).read_bytes(), payload)

    def test_independent_offline_changes_merge_on_both_computers(self):
        self.create(self.client_a)
        self.sync(self.a)
        self.clone_b()
        a_id = self.create(self.client_a, "idea", title="A 离线想法")
        b_id = self.create(self.client_b, "writing", title="B 离线草稿")
        self.sync(self.a)
        self.sync(self.b)
        self.sync(self.a)
        self.assertEqual(self.client_a.get(f"/entry/{b_id}").status_code, 200)
        self.assertEqual(self.client_b.get(f"/entry/{a_id}").status_code, 200)
        with self.connection(self.a) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM entries").fetchone()[0], 3)

    def test_same_note_conflict_keeps_both_versions_and_duplicate_attachments(self):
        note = self.create(self.client_a)
        self.post(self.client_a, f"/entry/{note}/upload", files=(io.BytesIO(b"results"), "结果.csv"))
        self.sync(self.a)
        self.clone_b()
        with self.connection(self.a) as db:
            version_a = db.execute("SELECT version FROM entries WHERE id=?", (note,)).fetchone()[0]
        with self.connection(self.b) as db:
            version_b = db.execute("SELECT version FROM entries WHERE id=?", (note,)).fetchone()[0]
        self.post(self.client_a, f"/entry/{note}/edit", title="科研笔记", status="进行中", content="A 的新发现", version=str(version_a))
        self.post(self.client_b, f"/entry/{note}/edit", title="科研笔记", status="进行中", content="B 的新发现", version=str(version_b))
        self.sync(self.a)
        with self.assertRaisesRegex(SyncError, "同一份资料"):
            self.sync(self.b)
        paths = self.ws_b.conflicts()
        self.assertEqual(paths, [f"workspace/records/{note}.md"])
        self.assertIn("B 的新发现".encode(), self.client_b.get(f"/entry/{note}").data)
        blocked = self.post(self.client_b, f"/entry/{note}/edit", title="不应覆盖", status="进行中", content="blocked", version="2")
        self.assertEqual(blocked.status_code, 409)
        self.ws_b.resolve(paths[0], "both")
        with self.connection(self.b) as db:
            self.ws_b.refresh(db)
            contents = {row[0] for row in db.execute("SELECT content FROM entries")}
            self.assertEqual(contents, {"A 的新发现", "B 的新发现"})
            self.assertEqual(db.execute("SELECT count(*) FROM attachments").fetchone()[0], 2)
        self.sync(self.b)
        self.sync(self.a)
        with self.connection(self.a) as db:
            self.assertEqual({row[0] for row in db.execute("SELECT content FROM entries")}, {"A 的新发现", "B 的新发现"})
        self.assertFalse(self.ws_b.conflicts())

    def test_plain_git_pull_is_loaded_into_existing_webpage(self):
        note = self.create(self.client_a)
        self.sync(self.a)
        self.clone_b()
        with self.connection(self.a) as db:
            version = db.execute("SELECT version FROM entries WHERE id=?", (note,)).fetchone()[0]
        self.post(self.client_a, f"/entry/{note}/edit", title="Git 拉取的新标题", status="已完成", content="拉取后的正文", version=str(version))
        self.sync(self.a)
        self.ws_b.git("pull", "--ff-only", "origin", "main")
        self.assertIn("拉取后的正文".encode(), self.client_b.get(f"/entry/{note}").data)

    def test_failure_preserves_local_commit_and_does_not_mark_success(self):
        note = self.create(self.client_a)
        original = self.ws_a.git
        def failing_git(*args, **kwargs):
            if args and args[0] == "fetch":
                raise SyncError("模拟离线")
            return original(*args, **kwargs)
        self.ws_a.git = failing_git
        with self.assertRaisesRegex(SyncError, "模拟离线"):
            self.sync(self.a)
        self.ws_a.git = original
        self.assertIn(note, self.ws_a.git("ls-tree", "-r", "--name-only", "HEAD").stdout)
        with self.connection(self.a) as db:
            self.assertIsNone(db.execute("SELECT value FROM settings WHERE key='last_git_sync'").fetchone())
        self.sync(self.a)

    def test_invalid_remote_and_malformed_records_are_not_applied(self):
        for url in ["https://token@github.com/user/repo.git", "file:///tmp/repo", "https://elsewhere.example/user/repo", "--upload-pack=evil"]:
            with self.assertRaises(SyncError):
                self.ws_a.configure(url, "main")
        note = self.create(self.client_a)
        record_file = self.ws_a.records / f"{note}.md"
        record = decode(record_file.read_bytes())
        record["metadata"]["url"] = "javascript:alert(1)"
        record_file.write_bytes(encode(record))
        with self.connection(self.a) as db:
            with self.assertRaises(SyncError):
                self.ws_a.refresh(db)
            self.assertEqual(json.loads(db.execute("SELECT metadata FROM entries WHERE id=?", (note,)).fetchone()[0])["url"], "")

    def test_checkpoint_does_not_commit_unrelated_staged_code(self):
        self.create(self.client_a)
        with (self.repo_a / "app.py").open("a") as stream:
            stream.write("\n# unrelated local edit\n")
        self.ws_a.git("add", "--", "app.py")
        self.ws_a.checkpoint()
        committed = self.ws_a.git("show", "--format=", "--name-only", "HEAD").stdout
        self.assertNotIn("app.py", committed)
        self.assertIn("app.py", self.ws_a.git("diff", "--cached", "--name-only").stdout)

    def test_windows_line_endings_and_restoring_missing_local_attachment(self):
        note = self.create(self.client_a)
        self.post(self.client_a, f"/entry/{note}/upload", files=(io.BytesIO(b"pdf data"), "论文:含Windows特殊字符.pdf"))
        file_path = self.ws_a.records / f"{note}.md"
        raw = file_path.read_bytes()
        self.assertEqual(decode(raw)["content"], decode(raw.replace(b"\n", b"\r\n"))["content"])
        with self.connection(self.a) as db:
            file = db.execute("SELECT * FROM attachments WHERE entry_id=?", (note,)).fetchone()
            cached = self.a.config["DATA_DIR"] / "uploads" / file["storage_name"]
            cached.unlink()
            self.ws_a.stamps.clear()
            self.ws_a.refresh(db)
            self.assertEqual(cached.read_bytes(), b"pdf data")

    def test_web_sync_endpoint_records_success_and_requires_csrf(self):
        self.create(self.client_a)
        self.assertEqual(self.client_a.post("/sync/run").status_code, 400)
        response = self.post(self.client_a, "/sync/run")
        self.assertEqual(response.status_code, 302)
        page = self.client_a.get("/sync")
        self.assertIn("已同步到 GitHub".encode(), page.data)
        with self.connection(self.a) as db:
            self.assertIsNotNone(db.execute("SELECT value FROM settings WHERE key='last_git_sync'").fetchone())


if __name__ == "__main__":
    unittest.main()
