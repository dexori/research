import io
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing

from app import create_app


class ResearchHubTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="research-test-")
        self.app = create_app(self.temp.name, testing=True)
        self.client = self.app.test_client()
        self.client.get("/setup")
        self.post("/setup", password="test-password-2026", confirm="test-password-2026")

    def tearDown(self):
        self.temp.cleanup()

    def post(self, route, **data):
        with self.client.session_transaction() as session:
            token = session.get("csrf")
        if not token:
            self.client.get(route if route in {"/setup", "/login"} else "/")
            with self.client.session_transaction() as session:
                token = session["csrf"]
        return self.client.post(route, data={"csrf": token, **data})

    def make(self, kind, **data):
        response = self.post(f"/new/{kind}", title="测试记录", status="进行中", content="## 实际笔记\n\n已保存", **data)
        self.assertEqual(response.status_code, 302)
        return response.location.split("/")[-1]

    def test_private_routes_require_password_and_csrf(self):
        other = self.app.test_client()
        for url in ["/", "/library/paper", "/backup/download", "/experiments.csv", "/attachment/abc"]:
            self.assertEqual(other.get(url).location, "/login")
        response = self.client.post("/new/paper", data={"title": "CSRF attack"})
        self.assertEqual(response.status_code, 400)
        response = self.post("/logout")
        self.assertEqual(response.location, "/login")
        self.client.get("/login")
        bad = self.post("/login", password="incorrect")
        self.assertIn("密码不正确".encode(), bad.data)
        self.assertEqual(self.post("/login", password="test-password-2026").location, "/")

    def test_all_record_types_project_relation_and_persistence(self):
        project = self.make("project")
        ids = [self.make(kind, project_id=project, tags="VLA，LIBERO") for kind in ["paper", "experiment", "dataset", "idea", "writing"]]
        for identifier in ids:
            self.assertEqual(self.client.get(f"/entry/{identifier}").status_code, 200)
        self.assertIn("项目资料".encode(), self.client.get(f"/entry/{project}").data)
        for kind in ["project", "paper", "experiment", "dataset", "idea", "writing"]:
            self.assertEqual(self.client.get(f"/library/{kind}").status_code, 200)
        restarted = create_app(self.temp.name, testing=True)
        with closing(sqlite3.connect(Path(self.temp.name) / "research.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM entries").fetchone()[0], 6)
        self.assertEqual(restarted.config["SECRET_KEY"], self.app.config["SECRET_KEY"])

    def test_conflict_preserves_input_and_does_not_overwrite(self):
        identifier = self.make("paper")
        response = self.post(f"/entry/{identifier}/edit", title="电脑 A 修改", status="已完成", content="new", version="1")
        self.assertEqual(response.status_code, 302)
        response = self.post(f"/entry/{identifier}/edit", title="电脑 B 待合并", status="进行中", content="未保存的输入", version="1")
        self.assertEqual(response.status_code, 409)
        self.assertIn("未保存的输入".encode(), response.data)
        self.assertIn("另一台电脑更新".encode(), response.data)
        view = self.client.get(f"/entry/{identifier}")
        self.assertIn("电脑 A 修改".encode(), view.data)
        self.assertNotIn("未保存的输入".encode(), view.data)

    def test_upload_download_markdown_and_full_backup_restore(self):
        identifier = self.make("paper")
        payload = b"%PDF-1.4 research attachment"
        response = self.post(f"/entry/{identifier}/upload", files=(io.BytesIO(payload), "文献.pdf"))
        self.assertEqual(response.status_code, 302)
        page = self.client.get(f"/entry/{identifier}").data
        attachment_id = re.search(rb'/attachment/([a-f0-9]+)', page)[1].decode()
        download = self.client.get(f"/attachment/{attachment_id}")
        self.assertEqual(download.data, payload)
        self.assertTrue(download.headers["Content-Disposition"].startswith("attachment;"))
        download.close()
        self.assertIn("实际笔记".encode(), self.client.get(f"/entry/{identifier}/export").data)
        response = self.client.get("/backup/download")
        self.assertEqual(response.status_code, 200)
        backup_bytes = response.data
        response.close()
        with zipfile.ZipFile(io.BytesIO(backup_bytes)) as archive:
            records = json.loads(archive.read("records.json"))
            self.assertEqual(records[0]["id"], identifier)
            names = archive.namelist()
            self.assertNotIn("session.key", names)
            attachment_path = next(name for name in names if name.startswith("attachments/"))
            self.assertEqual(archive.read(attachment_path), payload)
            with tempfile.TemporaryDirectory(prefix="research-restore-") as restored:
                root = Path(restored)
                (root / "research.sqlite3").write_bytes(archive.read("research.sqlite3"))
                (root / "uploads").mkdir()
                (root / "uploads" / attachment_path.split("/")[-1]).write_bytes(payload)
                restored_app = create_app(root, testing=True)
                client = restored_app.test_client()
                client.get("/login")
                with client.session_transaction() as session:
                    token = session["csrf"]
                self.assertEqual(client.post("/login", data={"csrf": token, "password": "test-password-2026"}).status_code, 302)
                self.assertIn("实际笔记".encode(), client.get(f"/entry/{identifier}").data)
                restored_download = client.get(f"/attachment/{attachment_id}")
                self.assertEqual(restored_download.data, payload)
                restored_download.close()

    def test_markdown_sanitization_and_url_validation(self):
        response = self.post("/new/paper", title="链接验证", status="待开始", url="javascript:alert(1)")
        self.assertEqual(response.status_code, 200)
        self.assertIn("完整的 http".encode(), response.data)
        identifier = self.make("paper")
        self.post(f"/entry/{identifier}/edit", title="安全测试", status="待开始", version="1", content='<script>alert(1)</script>\n\n[bad](javascript:alert(1))\n\n**good**')
        page = self.client.get(f"/entry/{identifier}").data
        self.assertNotIn(b"<script>alert", page)
        self.assertNotIn(b'href="javascript:', page)
        self.assertIn(b"<strong>good</strong>", page)

    def test_search_literal_filters_and_archive(self):
        identifier = self.make("paper", tags="VLA, LIBERO")
        self.assertIn(identifier.encode(), self.client.get("/search?q=VLA").data)
        self.assertNotIn(identifier.encode(), self.client.get("/search?q=%25").data)
        self.assertIn(identifier.encode(), self.client.get("/library/paper?q=LIBERO&status=进行中").data)
        self.post(f"/entry/{identifier}/archive")
        self.assertIn(identifier.encode(), self.client.get("/library/paper?status=已归档").data)
        self.post(f"/entry/{identifier}/archive")
        self.assertIn(identifier.encode(), self.client.get("/library/paper?status=进行中").data)

    def test_experiment_csv_and_finite_metric(self):
        response = self.post("/new/experiment", title="bad", status="进行中", metric_value="NaN")
        self.assertIn("有限数字".encode(), response.data)
        self.make("experiment", metric_name="Success rate (%)", metric_value="76", model="SmolVLA", seed="0", params="lr: 1e-4")
        output = self.client.get("/experiments.csv").data.decode("utf-8-sig")
        self.assertIn("SmolVLA", output)
        self.assertIn("Success rate (%)", output)
        self.assertIn("76", output)

    def test_remote_setup_rejected_and_upload_limit(self):
        with tempfile.TemporaryDirectory(prefix="research-unconfigured-") as empty:
            client = create_app(empty, testing=True).test_client()
            self.assertEqual(client.get("/setup", environ_overrides={"REMOTE_ADDR": "192.168.1.20"}).status_code, 403)
        self.app.config["MAX_CONTENT_LENGTH"] = 128
        response = self.post("/entry/unknown/upload", files=(io.BytesIO(b"a" * 300), "file.csv"))
        self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main()
