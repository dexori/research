"""A private, single-owner research workspace with server-side persistence."""
from __future__ import annotations

import argparse
import csv
import getpass
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import uuid
import zipfile
from datetime import datetime, timezone
from contextlib import closing
from urllib.parse import urlsplit

import bleach
from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
import markdown
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash
from git_sync import GitWorkspace, SyncError
from github_auth import GitHubAuth

ROOT = Path(__file__).resolve().parent
KINDS = {
    "project": ("研究项目", "项目", "◈"),
    "paper": ("文献笔记", "文献", "▤"),
    "experiment": ("实验记录", "实验", "⌁"),
    "dataset": ("数据与模型", "数据", "▦"),
    "idea": ("研究想法", "想法", "✧"),
    "writing": ("论文写作", "草稿", "¶"),
}
STATUSES = ["待开始", "进行中", "已完成", "已归档"]
TEMPLATES = {
    "project": "## 研究问题\n\n\n## 假设\n\n\n## 相关工作\n\n\n## 实验计划\n\n\n## 下一步\n\n- [ ] ",
    "paper": "## 一句话总结\n\n\n## 研究问题与动机\n\n\n## 方法\n\n\n## 主要结果\n\n\n## 局限\n\n\n## 对我的研究有什么启发\n\n",
    "experiment": "## 实验目的\n\n\n## 环境与运行命令\n\n```bash\n\n```\n\n## 观察与结果\n\n\n## 结论与下一步\n\n",
    "dataset": "## 来源与用途\n\n\n## 版本与划分\n\n\n## 使用方法\n\n\n## 注意事项\n\n",
    "idea": "## 问题\n\n\n## 为什么值得研究\n\n\n## 初步假设\n\n\n## 如何验证\n\n",
    "writing": "## 摘要\n\n\n## 引言\n\n\n## 相关工作\n\n\n## 方法\n\n\n## 实验\n\n\n## 结论\n\n",
}


def create_app(data_dir=None, testing=False, repo_dir=None):
    app = Flask(__name__)
    directory = Path(data_dir or os.environ.get("RESEARCH_DATA_DIR", ROOT / "data")).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    (directory / "uploads").mkdir(exist_ok=True, mode=0o700)
    workspace = GitWorkspace(repo_dir or (directory / "checkout" if testing else ROOT), directory)
    app.extensions["git_workspace"] = workspace
    github = GitHubAuth(workspace, auto_check=not testing)
    app.extensions["github_auth"] = github
    secret_path = directory / "session.key"
    try:
        fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_hex(32))
    app.config.update(SECRET_KEY=secret_path.read_text(), DATA_DIR=directory,
                      MAX_CONTENT_LENGTH=64 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=os.environ.get("RESEARCH_HTTPS") == "1",
                      TESTING=testing, PERMANENT_SESSION_LIFETIME=3600 * 24 * 7)
    attempts = {}

    def db():
        if "db" not in g:
            g.db = sqlite3.connect(directory / "research.sqlite3", timeout=15)
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys=ON")
        return g.db

    @app.teardown_appcontext
    def close_db(error):
        connection = g.pop("db", None)
        if connection:
            connection.close()
        if g.pop("workspace_locked", False):
            workspace.lock.release()

    with app.app_context():
        db().executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS entries (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                status TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '',
                project_id TEXT REFERENCES entries(id) ON DELETE SET NULL,
                content TEXT NOT NULL DEFAULT '', metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_entries_kind_updated ON entries(kind, updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_entries_project ON entries(project_id);
            CREATE TABLE IF NOT EXISTS attachments (
                id TEXT PRIMARY KEY, entry_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
                name TEXT NOT NULL, storage_name TEXT NOT NULL UNIQUE, size INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_files (entry_id TEXT PRIMARY KEY, hash TEXT NOT NULL);
        """)
        db().commit()
        try:
            workspace.migrate(db())
        except (SyncError, OSError) as error:
            db().rollback()
            workspace.error = str(error)

    def setting(key):
        row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def csrf():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    @app.before_request
    def protect():
        if request.endpoint == "static":
            return
        workspace.lock.acquire()
        g.workspace_locked = True
        configured = setting("password_hash")
        if request.endpoint not in {"login", "setup"}:
            if not configured:
                return redirect(url_for("setup"))
            if not session.get("authenticated") or session.get("auth_version") != setting("auth_version"):
                return redirect(url_for("login"))
        if request.method == "POST":
            token = request.form.get("csrf", "")
            if not token or not secrets.compare_digest(token, session.get("csrf", "")):
                abort(400, "页面已过期，请刷新后重试。")
        if request.endpoint not in {"login", "setup", "logout"}:
            try:
                workspace.refresh(db())
            except (SyncError, OSError) as error:
                db().rollback()
                workspace.error = str(error)
            if request.method == "POST" and request.endpoint not in {"sync_configure", "sync_run", "sync_resolve", "github_login"}:
                if workspace.conflicts() or workspace.error:
                    abort(409, "同步资料需要检查。请先打开「GitHub 同步」处理冲突或附件缺失，再编辑记录。")

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.context_processor
    def context():
        return {"kinds": KINDS, "statuses": STATUSES, "csrf": csrf,
                "today": datetime.now().strftime("%Y年%m月%d日"), "app_name": "研序 Research Hub",
                "workspace_error": workspace.error}

    def now():
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")

    def get_entry(identifier):
        row = db().execute("SELECT * FROM entries WHERE id=?", (identifier,)).fetchone()
        if row is None:
            abort(404)
        entry = dict(row)
        entry["meta"] = json.loads(entry["metadata"])
        return entry

    def projects():
        return db().execute("SELECT id,title FROM entries WHERE kind='project' ORDER BY title").fetchall()

    def render_md(text):
        html = markdown.markdown(text, extensions=["fenced_code", "tables", "sane_lists"])
        return Markup(bleach.clean(html, tags={"p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "a", "strong", "em", "del", "blockquote", "ul", "ol", "li", "code", "pre", "table", "thead", "tbody", "tr", "th", "td"},
                                 attributes={"a": ["href", "title"], "code": ["class"]}, protocols=["http", "https", "mailto"], strip=True))

    app.jinja_env.filters["markdown"] = render_md
    app.jinja_env.filters["day"] = lambda value: value[:10]
    app.jinja_env.filters["filesize"] = lambda value: f"{value / 1024 / 1024:.1f} MB" if value >= 1024 * 1024 else f"{value / 1024:.1f} KB"

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if setting("password_hash"):
            return redirect(url_for("login"))
        if request.remote_addr not in {"127.0.0.1", "::1"}:
            abort(403, "请先在服务器本机设置访问密码。")
        if request.method == "POST":
            password = request.form.get("password", "")
            if len(password) < 10 or password != request.form.get("confirm"):
                flash("密码至少 10 个字符，两次输入需一致。", "error")
            else:
                db().execute("INSERT INTO settings VALUES ('password_hash',?)", (generate_password_hash(password),))
                version = secrets.token_hex(16)
                db().execute("INSERT INTO settings VALUES ('auth_version',?)", (version,))
                db().commit()
                session.clear()
                session.update(authenticated=True, auth_version=version)
                session.permanent = True
                return redirect(url_for("home"))
        return render_template("auth.html", setup=True)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not setting("password_hash"):
            return redirect(url_for("setup"))
        if request.method == "POST":
            address = request.remote_addr
            times = [t for t in attempts.get(address, []) if t > time.monotonic() - 300]
            if len(times) >= 10:
                flash("尝试次数过多，请 5 分钟后再试。", "error")
                return render_template("auth.html", setup=False), 429
            if check_password_hash(setting("password_hash"), request.form.get("password", "")):
                attempts.pop(address, None)
                session.clear()
                session.update(authenticated=True, auth_version=setting("auth_version"))
                session.permanent = True
                return redirect(url_for("home"))
            attempts[address] = times + [time.monotonic()]
            flash("密码不正确。", "error")
        return render_template("auth.html", setup=False)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def home():
        counts = {row["kind"]: row["total"] for row in db().execute("SELECT kind,count(*) total FROM entries GROUP BY kind")}
        recent = db().execute("SELECT * FROM entries ORDER BY updated_at DESC LIMIT 8").fetchall()
        active = db().execute("SELECT e.*, (SELECT count(*) FROM entries c WHERE c.project_id=e.id) child_count FROM entries e WHERE kind='project' AND status!='已归档' ORDER BY updated_at DESC LIMIT 4").fetchall()
        experiments = [dict(row) for row in db().execute("SELECT * FROM entries WHERE kind='experiment' ORDER BY updated_at DESC LIMIT 6")]
        for entry in experiments:
            entry["meta"] = json.loads(entry["metadata"])
        return render_template("home.html", active="home", counts=counts, recent=recent, projects=active, experiments=experiments)

    @app.get("/library/<kind>")
    def library(kind):
        if kind not in KINDS:
            abort(404)
        query, status, project = request.args.get("q", "").strip(), request.args.get("status", ""), request.args.get("project", "")
        sql, params = "SELECT e.*,p.title project_title FROM entries e LEFT JOIN entries p ON e.project_id=p.id WHERE e.kind=?", [kind]
        if query:
            sql += " AND (e.title LIKE ? ESCAPE '\\' OR e.tags LIKE ? ESCAPE '\\' OR e.content LIKE ? ESCAPE '\\')"
            needle = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params.extend([needle] * 3)
        if status:
            sql += " AND e.status=?"
            params.append(status)
        if project:
            sql += " AND e.project_id=?"
            params.append(project)
        rows = [dict(row) for row in db().execute(sql + " ORDER BY e.updated_at DESC", params)]
        for row in rows:
            row["meta"] = json.loads(row["metadata"])
        return render_template("library.html", active=kind, kind=kind, entries=rows, projects=projects(), query=query, status=status, project=project)

    @app.get("/search")
    def search():
        query = request.args.get("q", "").strip()
        needle = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = db().execute("SELECT * FROM entries WHERE title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\' ORDER BY updated_at DESC LIMIT 100", [needle] * 3).fetchall() if query else []
        return render_template("search.html", active="search", entries=rows, query=query)

    def save_entry(kind, current=None):
        title = request.form.get("title", "").strip()
        content = request.form.get("content", "")
        if not title or len(title) > 240:
            raise ValueError("请填写标题，最多 240 个字符。")
        if len(content) > 2_000_000:
            raise ValueError("笔记过长，请拆分为多个记录。")
        status = request.form.get("status", "待开始")
        if status not in STATUSES:
            raise ValueError("请选择有效状态。")
        project_id = request.form.get("project_id") or None
        if kind == "project":
            project_id = None
        if project_id and not db().execute("SELECT id FROM entries WHERE id=? AND kind='project'", (project_id,)).fetchone():
            raise ValueError("关联项目不存在。")
        tags = ", ".join(tag.strip() for tag in request.form.get("tags", "").replace("，", ",").split(",") if tag.strip())[:1000]
        fields = ["authors", "year", "url", "bibtex", "model", "dataset", "seed", "metric_name", "metric_value", "params", "code_version", "location", "data_version", "external_url"]
        meta = {field: request.form.get(field, "").strip() for field in fields}
        for key in ["url", "external_url"]:
            if meta[key] and (urlsplit(meta[key]).scheme not in {"http", "https"} or not urlsplit(meta[key]).netloc):
                raise ValueError("链接须为完整的 http:// 或 https:// 地址。")
        if meta["metric_value"]:
            try:
                value = float(meta["metric_value"])
                if not __import__("math").isfinite(value):
                    raise ValueError()
            except ValueError:
                raise ValueError("实验指标值须为有限数字，例如 0.76 或 76。")
        stamp = now()
        identifier = current["id"] if current else uuid.uuid4().hex
        values = (title, status, tags, project_id, content, json.dumps(meta, ensure_ascii=False), stamp)
        if current:
            try:
                version = int(request.form.get("version", "0"))
            except ValueError:
                version = 0
            result = db().execute("UPDATE entries SET title=?,status=?,tags=?,project_id=?,content=?,metadata=?,updated_at=?,version=version+1 WHERE id=? AND version=?", values + (identifier, version))
            if result.rowcount == 0:
                raise ValueError("这条记录已在另一台电脑更新。本页内容已保留，请复制后查看最新版本，避免覆盖。")
        else:
            db().execute("INSERT INTO entries (title,status,tags,project_id,content,metadata,updated_at,id,kind,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", values + (identifier, kind, stamp))
        try:
            workspace.write_entry(db(), identifier)
        except (OSError, SyncError) as error:
            raise ValueError("资料文件保存失败，请检查磁盘空间后重试。输入内容已保留。") from error
        db().commit()
        return identifier

    @app.route("/new/<kind>", methods=["GET", "POST"])
    def new(kind):
        if kind not in KINDS:
            abort(404)
        entry = {"content": TEMPLATES[kind], "status": "待开始", "project_id": request.args.get("project", ""), "meta": {}}
        if request.method == "POST":
            try:
                identifier = save_entry(kind)
                flash("已保存到本机。换电脑前，请在「GitHub 同步」上传最新资料。", "success")
                return redirect(url_for("detail", identifier=identifier))
            except ValueError as error:
                db().rollback()
                flash(str(error), "error")
                entry = {**request.form, "meta": dict(request.form)}
        return render_template("editor.html", active=kind, kind=kind, entry=entry, projects=projects(), editing=False)

    @app.route("/entry/<identifier>/edit", methods=["GET", "POST"])
    def edit(identifier):
        entry = get_entry(identifier)
        if request.method == "POST":
            try:
                save_entry(entry["kind"], entry)
                flash("修改已保存到本机。换电脑前，请同步到 GitHub。", "success")
                return redirect(url_for("detail", identifier=identifier))
            except ValueError as error:
                db().rollback()
                flash(str(error), "error")
                entry = {**entry, **request.form, "meta": dict(request.form)}
                return render_template("editor.html", active=entry["kind"], kind=entry["kind"], entry=entry, projects=projects(), editing=True), 409
        return render_template("editor.html", active=entry["kind"], kind=entry["kind"], entry=entry, projects=projects(), editing=True)

    @app.get("/entry/<identifier>")
    def detail(identifier):
        entry = get_entry(identifier)
        files = db().execute("SELECT * FROM attachments WHERE entry_id=? ORDER BY created_at", (identifier,)).fetchall()
        related = db().execute("SELECT * FROM entries WHERE project_id=? ORDER BY kind,updated_at DESC", (identifier,)).fetchall() if entry["kind"] == "project" else []
        parent = get_entry(entry["project_id"]) if entry["project_id"] else None
        return render_template("detail.html", active=entry["kind"], entry=entry, attachments=files, related=related, parent=parent)

    @app.post("/entry/<identifier>/archive")
    def archive(identifier):
        entry = get_entry(identifier)
        status = "进行中" if entry["status"] == "已归档" else "已归档"
        db().execute("UPDATE entries SET status=?,updated_at=?,version=version+1 WHERE id=?", (status, now(), identifier))
        workspace.write_entry(db(), identifier)
        db().commit()
        flash("已恢复。" if status == "进行中" else "已归档，仍可搜索和恢复。", "success")
        return redirect(url_for("detail", identifier=identifier))

    @app.post("/entry/<identifier>/upload")
    def upload(identifier):
        get_entry(identifier)
        files = [file for file in request.files.getlist("files") if file.filename]
        if not files:
            flash("请先选择文件。", "error")
            return redirect(url_for("detail", identifier=identifier))
        created = []
        try:
            for file in files:
                name = file.filename.replace("\\", "/").split("/")[-1][:240]
                if not name or any(ord(char) < 32 for char in name):
                    raise ValueError("文件名无效。")
                storage_name = uuid.uuid4().hex
                path = directory / "uploads" / storage_name
                created.append(path)
                file.save(path)
                db().execute("INSERT INTO attachments VALUES (?,?,?,?,?,?)", (uuid.uuid4().hex, identifier, name, storage_name, path.stat().st_size, now()))
            db().execute("UPDATE entries SET updated_at=?,version=version+1 WHERE id=?", (now(), identifier))
            workspace.write_entry(db(), identifier)
            db().commit()
        except (OSError, ValueError, SyncError):
            db().rollback()
            for path in created:
                path.unlink(missing_ok=True)
            flash("附件上传失败，请检查文件名与磁盘空间后重试。", "error")
        else:
            flash(f"已在本机保存 {len(files)} 个附件，同步到 GitHub 后可在其他电脑拉取。", "success")
        return redirect(url_for("detail", identifier=identifier))

    @app.get("/attachment/<identifier>")
    def attachment(identifier):
        row = db().execute("SELECT * FROM attachments WHERE id=?", (identifier,)).fetchone()
        if not row:
            abort(404)
        return send_file(directory / "uploads" / row["storage_name"], as_attachment=True, download_name=row["name"])

    def entry_markdown(entry):
        meta = json.loads(entry["metadata"])
        header = f"# {entry['title']}\n\n- ID: {entry['id']}\n- 类型: {KINDS[entry['kind']][0]}\n- 状态: {entry['status']}\n- 标签: {entry['tags']}\n- 更新: {entry['updated_at']}\n"
        if entry["project_id"]:
            header += f"- 项目 ID: {entry['project_id']}\n"
        for key, value in meta.items():
            if value:
                header += f"- {key}: {value}\n"
        files = db().execute("SELECT * FROM attachments WHERE entry_id=?", (entry["id"],)).fetchall()
        if files:
            header += "\n## 附件\n\n" + "\n".join(f"- [{file['name']}](../attachments/{file['storage_name']})" for file in files) + "\n"
        return header + "\n---\n\n" + entry["content"] + "\n"

    @app.get("/entry/<identifier>/export")
    def export_entry(identifier):
        entry = get_entry(identifier)
        return send_file(io.BytesIO(entry_markdown(entry).encode("utf-8")), as_attachment=True, download_name=f"{entry['kind']}-{identifier[:8]}.md", mimetype="text/markdown")

    @app.get("/experiments.csv")
    def export_csv():
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(["id", "title", "project", "status", "model", "dataset", "seed", "metric_name", "metric_value", "params", "code_version", "updated_at"])
        for row in db().execute("SELECT e.*,p.title project_title FROM entries e LEFT JOIN entries p ON p.id=e.project_id WHERE e.kind='experiment' ORDER BY e.updated_at DESC"):
            meta = json.loads(row["metadata"])
            cells = [row["id"], row["title"], row["project_title"] or "", row["status"]] + [meta.get(key, "") for key in ["model", "dataset", "seed", "metric_name", "metric_value", "params", "code_version"]] + [row["updated_at"]]
            # Spreadsheet programs can execute formulas in cells originating from user input.
            writer.writerow(["'" + str(cell) if str(cell).startswith(("=", "+", "-", "@", "\t", "\r")) else cell for cell in cells])
        return send_file(io.BytesIO(stream.getvalue().encode("utf-8-sig")), as_attachment=True, download_name="experiments.csv", mimetype="text/csv")

    @app.get("/backup")
    def backup():
        return render_template("backup.html", active="backup")

    @app.get("/sync")
    def sync_page():
        try:
            state = workspace.status()
        except SyncError as error:
            state = {"error": str(error), "connected": False, "remote": "", "branch": "main", "record_count": 0, "conflicts": [], "dirty": 0, "ahead": 0, "behind": 0}
        return render_template("sync.html", active="sync", state=state, last_sync=setting("last_git_sync"), github=app.extensions["github_auth"].snapshot())

    @app.post("/sync/github/login")
    def github_login():
        return jsonify(app.extensions["github_auth"].start())

    @app.get("/sync/github/status")
    def github_status():
        return jsonify(app.extensions["github_auth"].snapshot())

    @app.post("/sync/configure")
    def sync_configure():
        try:
            workspace.configure(request.form.get("remote", "").strip(), request.form.get("branch", "main").strip())
            flash("仓库地址已保存。点击「同步到 GitHub」拉取并上传资料。", "success")
        except (SyncError, OSError) as error:
            flash(str(error), "error")
        return redirect(url_for("sync_page"))

    @app.post("/sync/run")
    def sync_run():
        try:
            workspace.sync(db())
            flash("已同步到 GitHub。现在可以关闭这台电脑，在另一台电脑拉取并继续使用。", "success")
        except (SyncError, OSError) as error:
            db().rollback()
            flash(str(error), "error")
        return redirect(url_for("sync_page"))

    @app.post("/sync/resolve")
    def sync_resolve():
        try:
            workspace.resolve(request.form.get("path", ""), request.form.get("choice", "both"))
            workspace.refresh(db())
            flash("已处理这项冲突。完成所有冲突后，再点击同步上传。", "success")
        except (SyncError, OSError) as error:
            db().rollback()
            flash(str(error), "error")
        return redirect(url_for("sync_page"))

    @app.get("/backup/download")
    def download_backup():
        # A SQLite online backup gives the archive a consistent point-in-time snapshot.
        import tempfile
        with tempfile.TemporaryDirectory(prefix="research-backup-") as temporary:
            snapshot_path = Path(temporary) / "research.sqlite3"
            snapshot = sqlite3.connect(snapshot_path)
            snapshot.row_factory = sqlite3.Row
            db().backup(snapshot)
            # Larger backups spill to a temporary file rather than consuming RAM.
            output = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot_path, "research.sqlite3")
                rows = snapshot.execute("SELECT * FROM entries ORDER BY created_at").fetchall()
                archive.writestr("records.json", json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2))
                # All reads for Markdown and attachments use the same snapshot.
                original = g.db
                g.db = snapshot
                try:
                    for row in rows:
                        archive.writestr(f"{row['kind']}/{row['id']}.md", entry_markdown(row))
                    for file in snapshot.execute("SELECT * FROM attachments"):
                        archive.write(directory / "uploads" / file["storage_name"], f"attachments/{file['storage_name']}")
                finally:
                    g.db = original
                archive.writestr("RESTORE.txt", "停止服务后，将 research.sqlite3 放入一个新的 data 目录；将 attachments 目录改名为 uploads。使用 RESEARCH_DATA_DIR 指向该目录启动。原密码仍有效。请勿把含私人资料的备份上传公开仓库。\n")
            snapshot.close()
            output.seek(0)
        return send_file(output, as_attachment=True, download_name=f"research-hub-{datetime.now():%Y%m%d-%H%M%S}.zip", mimetype="application/zip")

    @app.errorhandler(413)
    def too_large(error):
        return render_template("error.html", message="一次上传最多 64 MB。大数据集和模型请在「数据与模型」中记录存储路径或链接。"), 413

    @app.errorhandler(404)
    def missing(error):
        return render_template("error.html", message="这条记录不存在。请返回工作台查找。"), 404

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(409)
    def bad_request(error):
        return render_template("error.html", message=error.description), error.code

    @app.errorhandler(sqlite3.OperationalError)
    def storage_error(error):
        app.logger.exception("Database operation failed")
        return render_template("error.html", message="暂时无法读写资料，请检查磁盘空间。若刚才在编辑，请返回上一页保留内容后再试。"), 503

    @app.errorhandler(SyncError)
    @app.errorhandler(OSError)
    def file_error(error):
        db().rollback()
        app.logger.exception("Workspace operation failed")
        return render_template("error.html", message="资料文件暂时无法保存或读取，请检查磁盘空间，并在「GitHub 同步」查看状态。"), 503

    return app


def main():
    parser = argparse.ArgumentParser(description="Private Research Hub")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--set-password", action="store_true", help="Set/reset password interactively")
    args = parser.parse_args()
    app = create_app()
    if args.set_password:
        password = getpass.getpass("新密码（至少 10 个字符）: ")
        if len(password) < 10 or password != getpass.getpass("确认密码: "):
            parser.error("密码太短或两次输入不一致。")
        with closing(sqlite3.connect(app.config["DATA_DIR"] / "research.sqlite3")) as connection:
            connection.execute("INSERT OR REPLACE INTO settings VALUES ('password_hash',?)", (generate_password_hash(password),))
            connection.execute("INSERT OR REPLACE INTO settings VALUES ('auth_version',?)", (secrets.token_hex(16),))
            connection.commit()
        print("密码已设置，现有登录已失效。")
        return
    with closing(sqlite3.connect(app.config["DATA_DIR"] / "research.sqlite3")) as connection:
        configured = connection.execute("SELECT value FROM settings WHERE key='password_hash'").fetchone()
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not configured:
        parser.error("请先运行 python app.py --set-password，再开启网络访问。")
    print(f"Research Hub: http://{args.host}:{args.port}", flush=True)
    from waitress import serve
    serve(app, host=args.host, port=args.port, threads=4)


if __name__ == "__main__":
    main()
