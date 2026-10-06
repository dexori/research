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
import threading
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
from git_sync import GitWorkspace, SyncError, atomic_write, UUID
from github_auth import GitHubAuth
from codex_bridge import CodexBridge, BridgeError, retrieve_notes, validate_selection, REASONING_EFFORTS
from draft_import import MAX_BYTES as DRAFT_BYTES, SUFFIXES, decode_text, stage_draft, load_draft, list_drafts
from math_compat import MathCompatExtension
from paper_reader import PaperReader, PaperError
from learning import CATEGORIES as KNOWLEDGE_CATEGORIES, prepare_card, PublicLearningSearch

ROOT = Path(__file__).resolve().parent
KINDS = {
    "project": ("研究项目", "项目", "◈"),
    "paper": ("文献笔记", "文献", "▤"),
    "knowledge": ("学习知识库", "知识卡", "▧"),
    "experiment": ("实验记录", "实验", "⌁"),
    "dataset": ("数据与模型", "数据", "▦"),
    "idea": ("研究想法", "想法", "✧"),
    "writing": ("论文写作", "草稿", "¶"),
}
STATUSES = ["待开始", "进行中", "已完成", "已归档"]
TEMPLATES = {
    "project": "## 研究问题\n\n\n## 假设\n\n\n## 相关工作\n\n\n## 实验计划\n\n\n## 下一步\n\n- [ ] ",
    "paper": "## 一句话总结\n\n\n## 研究问题与动机\n\n\n## 方法\n\n\n## 主要结果\n\n\n## 局限\n\n\n## 对我的研究有什么启发\n\n",
    "knowledge": "## 一句话理解\n\n\n## 前置知识\n\n\n## 核心原理与数据流\n\n\n## 训练与推理\n\n\n## 最小例子\n\n\n## 区别与联系\n\n\n## 常见误区\n\n\n## 自测问题\n\n\n## 待确认问题\n\n\n## 来源与阅读顺序\n\n",
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
    app.extensions["codex_bridge"] = CodexBridge(directory)
    app.extensions["paper_reader"] = PaperReader()
    app.extensions["learning_researcher"] = PublicLearningSearch()
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
            CREATE TABLE IF NOT EXISTS ai_threads (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                project_id TEXT REFERENCES entries(id) ON DELETE SET NULL,
                context_id TEXT REFERENCES entries(id) ON DELETE SET NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ai_messages (
                id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES ai_threads(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'complete', sources TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_messages_thread ON ai_messages(thread_id,created_at);
            CREATE TABLE IF NOT EXISTS draft_imports (
                draft_id TEXT PRIMARY KEY, entry_id TEXT NOT NULL REFERENCES entries(id), imported_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS learning_jobs (
                id TEXT PRIMARY KEY, topic TEXT NOT NULL, project_id TEXT REFERENCES entries(id) ON DELETE SET NULL,
                status TEXT NOT NULL, draft_id TEXT, error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS learning_progress (
                job_id TEXT PRIMARY KEY REFERENCES learning_jobs(id) ON DELETE CASCADE,
                state TEXT NOT NULL DEFAULT '{}'
            );
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
            if request.method == "POST" and request.endpoint not in {"sync_configure", "sync_run", "sync_resolve", "github_login", "assistant_send", "codex_check", "learning_start"}:
                if workspace.conflicts() or workspace.error:
                    abort(409, "同步资料需要检查。请先打开「GitHub 同步」处理冲突或附件缺失，再编辑记录。")

    @app.after_request
    def headers(response):
        response.headers['X-Research-Hub'] = '1'
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; style-src-attr 'unsafe-inline'; script-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.context_processor
    def context():
        return {"kinds": KINDS, "statuses": STATUSES, "csrf": csrf, "knowledge_categories": KNOWLEDGE_CATEGORIES,
                "today": datetime.now().strftime("%Y年%m月%d日"), "app_name": "研序 Research Hub",
                "workspace_error": workspace.error, "reasoning_efforts": REASONING_EFFORTS}

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

    def bridge_status():
        try:
            return app.extensions['codex_bridge'].status()
        except BridgeError as error:
            flash(str(error), 'error')
            return {'available': False, 'profile': 'Codex', 'model': '无法读取配置'}

    def codex_selection():
        model = request.form.get('model','').strip()
        if model=='__custom__':
            model = request.form.get('custom_model','').strip()
            if not model:
                raise BridgeError('请输入自定义模型名称。')
        return validate_selection(model,request.form.get('reasoning_effort','').strip())

    def learning_worker(identifier, topic, project_id, mode, selection):
        """Network work runs outside the workspace lock; only stage an unsaved draft."""
        def report(event):
            with app.app_context():
                row = db().execute("SELECT status FROM learning_jobs WHERE id=?", (identifier,)).fetchone()
                if not row or row['status']!='pending':
                    return
                save_learning_progress(identifier, event)
        try:
            bridge = app.extensions['codex_bridge']
            result = (app.extensions['learning_researcher'].research(topic, bridge, on_progress=report, **selection) if mode=='public'
                      else bridge.research_topic(topic, on_progress=report, **selection))
            report({'stage':'validating','detail':'正在检查检索记录与引用来源；草稿尚未保存。','output':result.get('content','')})
            card = prepare_card(topic, result)
            with app.app_context(), workspace.lock:
                job = db().execute('SELECT status FROM learning_jobs WHERE id=?', (identifier,)).fetchone()
                if not job or job['status']!='pending':
                    return
                draft = stage_draft(directory, card['content'], kind='knowledge', title=topic,
                                    project_id=project_id, tags=card['tags'],
                                    source_name='联网学习整理（AI 草稿）', metadata=card['metadata'])
                db().execute("UPDATE learning_jobs SET status='ready',draft_id=?,updated_at=? WHERE id=? AND status='pending'",
                             (draft['id'], now(), identifier))
                save_learning_progress(identifier, {'stage':'ready','detail':'草稿已生成，请审核后保存。','output':card['content']})
                db().commit()
        except Exception as error:
            # Do not expose raw CLI output, credentials, or unexpected exception details.
            message = str(error)[:1200] if isinstance(error, (BridgeError, ValueError)) else '整理未完成，请检查本机服务与磁盘后重试。'
            with app.app_context():
                db().execute("UPDATE learning_jobs SET status='error',error=?,updated_at=? WHERE id=? AND status='pending'",
                             (message, now(), identifier))
                save_learning_progress(identifier, {'stage':'error','detail':message})
                db().commit()

    def save_learning_progress(identifier, event):
        """Persist bounded public progress, not internal reasoning or tool transcripts."""
        row = db().execute('SELECT state FROM learning_progress WHERE job_id=?', (identifier,)).fetchone()
        state = json.loads(row['state']) if row else {}
        stages = {'queued','planning','searching','reading','generating','validating','ready','error'}
        stage = event.get('stage')
        if stage not in stages:
            return
        stamp = now()
        detail = str(event.get('detail',''))[:1200]
        events = state.get('events', [])
        if not events or (events[-1]['stage'],events[-1]['detail'])!=(stage,detail):
            events.append({'stage':stage,'detail':detail,'at':stamp})
        state.update(stage=stage, detail=detail, events=events[-60:], updated_at=stamp)
        if isinstance(event.get('selection'),dict):
            state['selection'] = {key:str(event['selection'].get(key,'跟随终端'))[:120] for key in ('model','reasoning_effort')}
        if isinstance(event.get('output'),str):
            state['output'] = event['output'][:60000]
        if isinstance(event.get('sources'),list):
            sources = []
            for source in event['sources'][:6]:
                if not isinstance(source,dict):
                    continue
                url = str(source.get('url',''))[:2000]
                try:
                    parsed = urlsplit(url)
                    safe = parsed.scheme=='https' and parsed.hostname and not parsed.username and not parsed.password
                except ValueError:
                    safe = False
                if safe:
                    sources.append({'title':str(source.get('title',''))[:500], 'url':url,
                                    'scope':str(source.get('scope',''))[:300]})
            state['sources'] = sources
        db().execute('INSERT INTO learning_progress VALUES (?,?) ON CONFLICT(job_id) DO UPDATE SET state=excluded.state',
                     (identifier,json.dumps(state,ensure_ascii=False)))
        db().commit()

    def expire_learning_jobs():
        cutoff = datetime.fromtimestamp(time.time()-540, timezone.utc).isoformat(timespec='microseconds')
        db().execute("UPDATE learning_jobs SET status='error',error='任务已中断或超时，请重新整理。未修改已有笔记。',updated_at=? WHERE status='pending' AND created_at<?",
                     (now(), cutoff))
        db().commit()

    @app.get('/learning')
    def learning_page():
        expire_learning_jobs()
        jobs = db().execute('SELECT * FROM learning_jobs ORDER BY created_at DESC LIMIT 20').fetchall()
        selected = next((job for job in jobs if job['id']==request.args.get('job')), jobs[0] if jobs else None)
        return render_template('learning.html', active='learning', bridge=bridge_status(), projects=projects(), jobs=jobs,
                               selected_job_url=url_for('learning_job',identifier=selected['id']) if selected else '')

    @app.post('/learning/start')
    def learning_start():
        try:
            selection = codex_selection()
        except BridgeError as error:
            return jsonify(error=str(error)), 400
        topic = request.form.get('topic', '').strip()
        project_id = request.form.get('project_id') or None
        mode = request.form.get('mode', 'public')
        if mode not in {'public','codex'}:
            return jsonify(error='请选择有效的检索方式。'), 400
        if not topic or len(topic)>240 or any(ord(char)<32 for char in topic):
            return jsonify(error='请输入学习主题，最多 240 字。'), 400
        if project_id and not db().execute("SELECT 1 FROM entries WHERE id=? AND kind='project'", (project_id,)).fetchone():
            return jsonify(error='关联研究项目不存在。'), 400
        expire_learning_jobs()
        if db().execute("SELECT 1 FROM learning_jobs WHERE status='pending'").fetchone():
            return jsonify(error='已有学习主题正在整理，请等待完成。'), 409
        try:
            if not app.extensions['codex_bridge'].status()['available']:
                return jsonify(error='未找到 Codex CLI，请先在「科研助手」查看新电脑连接说明。'), 503
        except BridgeError as error:
            return jsonify(error=str(error)), 503
        identifier, stamp = uuid.uuid4().hex, now()
        db().execute('INSERT INTO learning_jobs VALUES (?,?,?,?,?,?,?,?)',
                     (identifier, topic, project_id, 'pending', None, '', stamp, stamp))
        save_learning_progress(identifier, {'stage':'queued','detail':'主题已提交，准备检索资料。', 'selection':selection})
        db().commit()
        try:
            threading.Thread(target=learning_worker, args=(identifier, topic, project_id, mode, selection), daemon=True).start()
        except RuntimeError:
            db().execute("UPDATE learning_jobs SET status='error',error='无法启动整理任务，请重试。' WHERE id=?", (identifier,))
            db().commit()
            return jsonify(error='无法启动整理任务，请重试。'), 503
        return jsonify(job_url=url_for('learning_job', identifier=identifier)), 202

    @app.get('/learning/job/<identifier>')
    def learning_job(identifier):
        expire_learning_jobs()
        row = db().execute('SELECT * FROM learning_jobs WHERE id=?', (identifier,)).fetchone()
        if not row:
            abort(404)
        saved = db().execute('SELECT state FROM learning_progress WHERE job_id=?', (identifier,)).fetchone()
        progress = json.loads(saved['state']) if saved else {'stage':'queued','detail':'此任务尚无过程记录。','events':[]}
        if row['status']=='error':
            progress.update(stage='error',detail=row['error'])
        elif row['status']=='ready':
            progress.update(stage='ready',detail='草稿已生成，请审核后保存。')
        output = progress.pop('output','')
        progress['output_html'] = str(render_md(output)) if output else ''
        end = row['updated_at'] if row['status']!='pending' else now()
        try:
            elapsed = max(0,int((datetime.fromisoformat(end)-datetime.fromisoformat(row['created_at'])).total_seconds()))
        except (ValueError, TypeError):
            elapsed = 0
        return jsonify(status=row['status'], topic=row['topic'], error=row['error'],
                       progress=progress, elapsed_seconds=elapsed,
                       review_url=url_for('import_review', identifier=row['draft_id']) if row['status']=='ready' else None)

    def paper_source(entry):
        """Only accept a cache owned by this record and matching its current URL."""
        name = entry['meta'].get('paper_source', '')
        source = None
        if UUID.fullmatch(name):
            row = db().execute('SELECT * FROM attachments WHERE entry_id=? AND storage_name=?',
                               (entry['id'], name)).fetchone()
            path = directory / 'uploads' / name
            if row and path.is_file() and not path.is_symlink() and path.stat().st_size <= 1500000:
                try:
                    payload = json.loads(path.read_text(encoding='utf-8'))
                    if (payload.get('entry_id') == entry['id'] and payload.get('requested_url') == entry['meta'].get('url', '')
                            and isinstance(payload.get('text'), str) and 200 <= len(payload['text']) <= 180000):
                        source = payload
                except (OSError, ValueError, AttributeError):
                    pass
        if source:
            return {'status': 'ready', 'message': f"已提取正文 · {len(source['text']):,} 字符", 'source': source}
        if entry['meta'].get('paper_source_error'):
            try:
                failure = json.loads(entry['meta']['paper_source_error'])
                if failure.get('url') == entry['meta'].get('url', ''):
                    return {'status': 'error', 'message': failure['message']}
            except (ValueError, KeyError, AttributeError):
                pass
        return {'status': 'missing', 'message': '尚未读取论文正文；链接本身不等于正文。'}

    def read_paper(entry, attachment_id=None):
        """Do network/parser work without holding the research workspace lock."""
        original_url = entry['meta'].get('url', '')
        previous_source = entry['meta'].get('paper_source', '')
        raw, attachment_name = None, None
        if attachment_id:
            row = db().execute('SELECT * FROM attachments WHERE id=? AND entry_id=?',
                               (attachment_id, entry['id'])).fetchone()
            if not row or not row['name'].lower().endswith('.pdf'):
                raise PaperError('请选择这篇文献的 PDF 附件。')
            path = directory / 'uploads' / row['storage_name']
            if row['size'] > 20 * 1024 * 1024 or path.is_symlink():
                raise PaperError('PDF 超过 20 MB 读取上限或路径无效。')
            raw = path.read_bytes()
            attachment_name = row['name']
        elif not original_url:
            raise PaperError('请先填写论文链接，或选择已上传的 PDF。')
        workspace.lock.release()
        g.workspace_locked = False
        result, failure = None, None
        try:
            result = app.extensions['paper_reader'].read(url=original_url, raw=raw)
        except PaperError as error:
            failure = str(error)
        finally:
            workspace.lock.acquire()
            g.workspace_locked = True
        workspace.refresh(db())
        current = get_entry(entry['id'])
        if current['meta'].get('url', '') != original_url or current['meta'].get('paper_source', '') != previous_source:
            raise PaperError('读取期间链接或正文缓存已更新，请刷新页面；未覆盖新资料。')
        meta = dict(current['meta'])
        stamp = now()
        if failure:
            meta['paper_source_error'] = json.dumps({'url': original_url, 'message': failure}, ensure_ascii=False)
        else:
            result.update(entry_id=entry['id'], requested_url=original_url, read_at=stamp,
                          attachment_name=attachment_name)
            storage_name = uuid.uuid4().hex
            raw_cache = json.dumps(result, ensure_ascii=False).encode('utf-8')
            atomic_write(directory / 'uploads' / storage_name, raw_cache)
            db().execute('INSERT INTO attachments VALUES (?,?,?,?,?,?)',
                         (uuid.uuid4().hex, entry['id'], '论文原文-提取.json', storage_name, len(raw_cache), stamp))
            meta['paper_source'] = storage_name
            meta.pop('paper_source_error', None)
        db().execute('UPDATE entries SET metadata=?,updated_at=?,version=version+1 WHERE id=?',
                     (json.dumps(meta, ensure_ascii=False), stamp, entry['id']))
        workspace.write_entry(db(), entry['id'])
        db().commit()
        if failure:
            raise PaperError(failure)
        return paper_source(get_entry(entry['id']))

    def render_md(text):
        html = markdown.markdown(text, extensions=["fenced_code", "tables", "sane_lists", "pymdownx.arithmatex", MathCompatExtension()],
                                 extension_configs={"pymdownx.arithmatex":{"generic":True}})
        def safe_attribute(tag, name, value):
            if tag=='a':
                return name in {'href','title'}
            if tag=='code':
                return name=='class'
            return tag in {'span','div'} and name=='class' and value=='arithmatex'
        return Markup(bleach.clean(html, tags={"p", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "a", "strong", "em", "del", "blockquote", "ul", "ol", "li", "code", "pre", "table", "thead", "tbody", "tr", "th", "td", "span", "div"},
                                 attributes=safe_attribute, protocols=["http", "https", "mailto"], strip=True))

    app.jinja_env.filters["markdown"] = render_md
    app.jinja_env.filters["day"] = lambda value: value[:10]
    app.jinja_env.filters["filesize"] = lambda value: f"{value / 1024 / 1024:.1f} MB" if value >= 1024 * 1024 else f"{value / 1024:.1f} KB"

    def chat_thread(identifier):
        row = db().execute('SELECT * FROM ai_threads WHERE id=?', (identifier,)).fetchone()
        if row is None:
            abort(404)
        return dict(row)

    def chat_messages(identifier):
        messages = []
        for row in db().execute('SELECT * FROM ai_messages WHERE thread_id=? ORDER BY created_at,id', (identifier,)):
            message = dict(row)
            message['sources'] = json.loads(message['sources'])
            message['html'] = str(render_md(message['content']))
            messages.append(message)
        return messages

    @app.get('/assistant')
    def assistant_page():
        identifier = request.args.get('thread')
        thread = chat_thread(identifier) if identifier else None
        context_id = thread['context_id'] if thread else request.args.get('entry')
        context = get_entry(context_id) if context_id else None
        threads = db().execute('SELECT * FROM ai_threads WHERE context_id=? ORDER BY updated_at DESC LIMIT 40',
                               (context_id,)).fetchall() if context_id else db().execute('SELECT * FROM ai_threads ORDER BY updated_at DESC LIMIT 40').fetchall()
        return render_template('assistant.html', active='assistant', bridge=bridge_status(),
                               thread=thread, threads=threads, messages=chat_messages(identifier) if thread else [],
                               projects=projects(), context=context, paper=paper_source(context) if context and context['kind']=='paper' else None)

    @app.get('/assistant/thread/<identifier>')
    def assistant_thread(identifier):
        thread = chat_thread(identifier)
        # A killed server must not leave a conversation waiting forever.
        cutoff = datetime.fromtimestamp(time.time()-240, timezone.utc).isoformat(timespec='microseconds')
        db().execute("UPDATE ai_messages SET status='error',content='回答已中断或超时，请重新发送问题。' WHERE thread_id=? AND status='pending' AND created_at<?",
                     (identifier,cutoff))
        db().commit()
        return jsonify(thread=thread,messages=chat_messages(identifier))

    @app.post('/assistant/connection/check')
    def codex_check():
        workspace.lock.release()
        g.workspace_locked = False
        return jsonify(app.extensions['codex_bridge'].login_status())

    @app.post('/assistant/send')
    def assistant_send():
        try:
            selection = codex_selection()
        except BridgeError as error:
            return jsonify(error=str(error)), 400
        question = request.form.get('message', '').strip()
        if not question or len(question) > 4000:
            return jsonify(error='请输入问题，最多 4000 字。'), 400
        bridge = app.extensions['codex_bridge']
        try:
            if not bridge.status()['available']:
                return jsonify(error='未找到 Codex CLI，请先安装并在终端登录。'), 503
        except BridgeError as error:
            return jsonify(error=str(error)), 503
        identifier = request.form.get('thread_id', '')
        if identifier:
            thread = chat_thread(identifier)
        else:
            project_id = request.form.get('project_id') or None
            context_id = request.form.get('context_id') or None
            if project_id and not db().execute("SELECT 1 FROM entries WHERE id=? AND kind='project'", (project_id,)).fetchone():
                return jsonify(error='选择的研究项目不存在。'), 400
            if context_id:
                context = get_entry(context_id)
                if project_id and context['id'] != project_id and context['project_id'] != project_id:
                    return jsonify(error='当前资料不属于所选项目。'), 400
            identifier = uuid.uuid4().hex
            stamp = now()
            db().execute('INSERT INTO ai_threads VALUES (?,?,?,?,?,?)',
                         (identifier, question[:70], project_id, context_id, stamp, stamp))
            db().commit()
            thread = chat_thread(identifier)
        if db().execute("SELECT 1 FROM ai_messages WHERE thread_id=? AND status='pending'", (identifier,)).fetchone():
            return jsonify(error='这段对话还有问题正在回答，请等待完成。'), 409
        previous = chat_messages(identifier)
        previous_ids = [source['id'] for message in previous[-4:] for source in message['sources']]
        sources = retrieve_notes(db(), question, thread['project_id'], previous_ids, thread['context_id'])
        paper_budget = 65000 if thread['context_id'] and sources and sources[0]['kind']=='paper' else 20000
        for source in sources:
            if source['kind'] != 'paper':
                continue
            entry = get_entry(source['id'])
            state = paper_source(entry)
            # Only the explicitly selected paper is downloaded, never all search hits.
            if source['id'] == thread['context_id'] and state['status'] == 'missing' and entry['meta'].get('url'):
                try:
                    state = read_paper(entry)
                except PaperError as error:
                    state = {'status': 'error', 'message': str(error)}
                except (OSError, SyncError):
                    db().rollback()
                    state = {'status': 'error', 'message': '原文缓存保存失败，本次只提供笔记；请检查磁盘与同步状态。'}
                refreshed = get_entry(source['id'])
                source.update(version=refreshed['version'], title=refreshed['title'], metadata=refreshed['meta'],
                              content=refreshed['content'][:10000], truncated=len(refreshed['content'])>10000)
            if state['status'] == 'ready':
                original = state['source']
                # Bound the context; disclose exactly which portion the model receives.
                limit = min(paper_budget, 65000 if source['id'] == thread['context_id'] else 8000)
                source['paper_text'] = original['text'][:limit]
                paper_budget -= len(source['paper_text'])
                source['paper_reading'] = {k: v for k, v in original.items() if k != 'text'}
                source['paper_reading'].update(sent_chars=len(source['paper_text']),
                                              context_truncated=len(original['text']) > limit)
            else:
                source['paper_reading'] = {'status': state['status'], 'message': state['message']}
        references = []
        for note in sources:
            reference = {key: note[key] for key in ['id','title','kind','version']}
            if 'paper_text' in note:
                reference.update(paper_chars=len(note['paper_text']), paper_truncated=note['paper_reading']['context_truncated'])
            references.append(reference)
        # Fetching a paper releases the lock; another turn may have started meanwhile.
        if db().execute("SELECT 1 FROM ai_messages WHERE thread_id=? AND status='pending'", (identifier,)).fetchone():
            return jsonify(error='这段对话已有问题正在回答，请等待完成。'), 409
        message_id, answer_id = uuid.uuid4().hex, uuid.uuid4().hex
        db().execute("INSERT INTO ai_messages (id,thread_id,role,content,created_at) VALUES (?,?,'user',?,?)",
                     (message_id, identifier, question, now()))
        db().execute("INSERT INTO ai_messages (id,thread_id,role,status,sources,created_at) VALUES (?,?,'assistant','pending',?,?)",
                     (answer_id, identifier, json.dumps(references,ensure_ascii=False), now()))
        db().execute('UPDATE ai_threads SET updated_at=? WHERE id=?', (now(),identifier))
        db().commit()
        history = [{'role': item['role'], 'content': item['content'][:2000]}
                   for item in previous if item['status']=='complete'][-10:]
        if thread['context_id'] and sources and sources[0]['kind']=='paper':
            history = [{'role': item['role'], 'content': item['content'][:2000]} for item in previous
                       if item['status']=='complete' and (item['role']=='user' or
                       all(s['id']==thread['context_id'] for s in item['sources']))][-10:]
        # Do not freeze the entire research workspace while Codex is thinking.
        workspace.lock.release()
        g.workspace_locked = False
        error = None
        try:
            answer = bridge.answer(question, history, sources, **selection)
        except BridgeError as failure:
            error = str(failure)
            answer = error
        except Exception:
            error = answer = '本次回答未完成，问题已保留。请检查 Codex 登录与网络后重试。'
        with workspace.lock:
            db().execute('UPDATE ai_messages SET content=?,status=? WHERE id=?',
                         (answer, 'error' if error else 'complete', answer_id))
            db().execute('UPDATE ai_threads SET updated_at=? WHERE id=?', (now(),identifier))
            db().commit()
            messages = chat_messages(identifier)
        selected = get_entry(thread['context_id']) if thread['context_id'] else None
        return jsonify(thread_id=identifier, messages=messages, error=error,
                       paper_status=paper_source(selected)['message'] if selected and selected['kind']=='paper' else None), 502 if error else 200

    @app.post('/assistant/draft')
    def assistant_draft():
        message = db().execute("SELECT * FROM ai_messages WHERE id=? AND role='assistant' AND status='complete'",
                               (request.form.get('message_id',''),)).fetchone()
        if message is None:
            abort(404)
        thread = chat_thread(message['thread_id'])
        target = request.form.get('target_id') or None
        if target:
            entry = get_entry(target)
            if target not in {source['id'] for source in json.loads(message['sources'])}:
                abort(400, '只能追加到本次提供的参考资料，请重新选择。')
            entry['content'] += '\n\n## 与 Codex 的讨论草稿（待审核）\n\n' + message['content']
            kind, editing = entry['kind'], True
            submit_url = url_for('edit',identifier=target)
        else:
            kind = request.form.get('kind','idea')
            if kind not in {'idea','paper','experiment','writing','knowledge'}:
                abort(400,'请选择有效的笔记类型。')
            entry = {'title':thread['title'], 'content':message['content'], 'status':'待开始',
                     'project_id':thread['project_id'], 'tags':'AI 草稿', 'meta':{}}
            editing, submit_url = False, url_for('new',kind=kind)
        flash('这是尚未保存的 AI 草稿。请核对事实与引用，编辑并点击保存后才会写入资料。','success')
        return render_template('editor.html', active=kind, kind=kind, entry=entry,
                               projects=projects(), editing=editing, submit_url=submit_url)

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
        category = request.args.get('category', '') if kind=='knowledge' else ''
        if category:
            if category not in KNOWLEDGE_CATEGORIES:
                abort(400, '请选择有效的知识分类。')
            sql += " AND json_extract(e.metadata,'$.knowledge_category')=?"
            params.append(category)
        rows = [dict(row) for row in db().execute(sql + " ORDER BY e.updated_at DESC", params)]
        for row in rows:
            row["meta"] = json.loads(row["metadata"])
        return render_template("library.html", active=kind, kind=kind, entries=rows, projects=projects(), query=query, status=status, project=project, category=category)

    @app.get("/search")
    def search():
        query = request.args.get("q", "").strip()
        needle = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = db().execute("SELECT * FROM entries WHERE title LIKE ? ESCAPE '\\' OR content LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\' ORDER BY updated_at DESC LIMIT 100", [needle] * 3).fetchall() if query else []
        return render_template("search.html", active="search", entries=rows, query=query)

    def save_entry(kind, current=None, import_id=None):
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
        if kind == 'knowledge':
            category = request.form.get('knowledge_category', '其他')
            if category not in KNOWLEDGE_CATEGORIES:
                raise ValueError('请选择有效的知识分类。')
            origin = current['meta'] if current else checked_draft(import_id).get('metadata', {}) if import_id else {}
            meta.update({key: origin[key] for key in ['learning_topic', 'search_date', 'search_count', 'search_method'] if key in origin})
            meta['knowledge_category'] = category
        if current:
            for field in ['paper_source', 'paper_source_error']:
                if field in current['meta']:
                    meta[field] = current['meta'][field]
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
        if import_id:
            db().execute('INSERT INTO draft_imports VALUES (?,?,?)',(import_id,identifier,stamp))
        db().commit()
        return identifier

    def imported_record(identifier):
        row = db().execute('SELECT entry_id FROM draft_imports WHERE draft_id=?',(identifier,)).fetchone()
        return row['entry_id'] if row else None

    def checked_draft(identifier):
        try:
            return load_draft(directory,identifier)
        except FileNotFoundError:
            abort(404)
        except ValueError as error:
            abort(400,str(error))

    @app.route('/imports',methods=['GET','POST'])
    def imports_page():
        if request.method=='POST':
            try:
                file = request.files.get('file')
                pasted = request.form.get('content','')
                if file and file.filename:
                    if pasted.strip():
                        raise ValueError('请选择上传文件或粘贴正文其中一种方式。')
                    source_name = file.filename.replace('\\','/').split('/')[-1]
                    if Path(source_name).suffix.lower() not in SUFFIXES:
                        raise ValueError('只接受 .md、.markdown 或 .txt 草稿文件。')
                    content = decode_text(file.stream.read(DRAFT_BYTES+1))
                else:
                    content = decode_text(pasted.encode('utf-8'))
                    source_name = '粘贴的讨论草稿.md'
                draft = stage_draft(directory,content,kind=request.form.get('kind','idea'),
                                    title=request.form.get('title',''),project_id=request.form.get('project_id') or None,
                                    tags=request.form.get('tags','终端导入'),source_name=source_name)
                return redirect(url_for('import_review',identifier=draft['id']))
            except ValueError as error:
                flash(str(error),'error')
        drafts = list_drafts(directory)
        for draft in drafts:
            draft['entry_id'] = imported_record(draft['id'])
        return render_template('imports.html',active='imports',drafts=drafts,projects=projects(),values=request.form)

    @app.get('/imports/<identifier>')
    def import_review(identifier):
        existing = imported_record(identifier)
        if existing:
            flash('这份草稿已导入，已打开原记录，不会重复创建。','success')
            return redirect(url_for('detail',identifier=existing))
        draft = checked_draft(identifier)
        kind = request.args.get('kind',draft['kind'])
        if kind not in KINDS:
            abort(400,'请选择有效的记录类型。')
        entry = {'title':draft['title'],'content':draft['content'],'status':'待开始',
                 'project_id':draft['project_id'],'tags':draft['tags'],'meta':draft.get('metadata', {})}
        if draft['project_id'] and not db().execute("SELECT 1 FROM entries WHERE id=? AND kind='project'",(draft['project_id'],)).fetchone():
            flash('草稿指定的项目在本机不存在，请重新选择关联项目。','error')
            entry['project_id'] = None
        return render_template('editor.html',active='imports',kind=kind,entry=entry,projects=projects(),
                               editing=False,submit_url=url_for('new',kind=kind),import_draft=draft)

    @app.route("/new/<kind>", methods=["GET", "POST"])
    def new(kind):
        if kind not in KINDS:
            abort(404)
        entry = {"content": TEMPLATES[kind], "status": "待开始", "project_id": request.args.get("project", ""), "meta": {}}
        draft = None
        if request.method == "POST":
            import_id = request.form.get('import_id') or None
            if import_id:
                existing = imported_record(import_id)
                if existing:
                    flash('草稿已导入，不会重复创建。','success')
                    return redirect(url_for('detail',identifier=existing))
                draft = checked_draft(import_id)
            try:
                identifier = save_entry(kind,import_id=import_id)
                flash("已保存到本机。换电脑前，请在「GitHub 同步」上传最新资料。", "success")
                return redirect(url_for("detail", identifier=identifier))
            except ValueError as error:
                db().rollback()
                flash(str(error), "error")
                entry = {**request.form, "meta": dict(request.form)}
        return render_template("editor.html", active=kind, kind=kind, entry=entry, projects=projects(), editing=False,import_draft=draft)

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
        thread, threads = None, []
        if entry['kind'] == 'paper':
            threads = db().execute('SELECT * FROM ai_threads WHERE context_id=? ORDER BY updated_at DESC LIMIT 40', (identifier,)).fetchall()
            requested = request.args.get('thread')
            if requested:
                thread = chat_thread(requested)
                if thread['context_id'] != identifier:
                    abort(404)
            elif request.args.get('discussion') != 'new' and threads:
                thread = dict(threads[0])
        return render_template("detail.html", active=entry["kind"], entry=entry, attachments=files, related=related, parent=parent,
                               context=entry, thread=thread, threads=threads, messages=chat_messages(thread['id']) if thread else [],
                               bridge=bridge_status() if entry['kind']=='paper' else None,
                               paper=paper_source(entry) if entry['kind']=='paper' else None)

    @app.post('/entry/<identifier>/paper/read')
    def paper_read(identifier):
        entry = get_entry(identifier)
        if entry['kind'] != 'paper':
            abort(400)
        try:
            read_paper(entry, request.form.get('attachment_id'))
            flash('论文正文已提取。不会改写笔记；同步 GitHub 后可在其他电脑使用。', 'success')
        except (PaperError, OSError, SyncError) as error:
            db().rollback()
            flash(str(error) if isinstance(error, PaperError) else '正文保存失败，请检查磁盘和同步状态后重试。', 'error')
        return redirect(url_for('detail', identifier=identifier) + '#paper-discussion')

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
                for path in (directory/'import-drafts').glob('*.json'):
                    try:
                        draft = load_draft(directory,path.stem)
                    except (ValueError,OSError):
                        continue
                    archive.writestr('import-drafts/'+draft['id']+'.json',json.dumps(draft,ensure_ascii=False))
                archive.writestr("RESTORE.txt", "停止服务后，将 research.sqlite3 放入一个新的 data 目录；将 attachments 目录改名为 uploads；如果存在 import-drafts 目录，也复制到该 data 目录。使用 RESEARCH_DATA_DIR 指向该目录启动。原密码仍有效。请勿把含私人资料的备份上传公开仓库。\n")
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
