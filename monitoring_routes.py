"""Authenticated monitoring endpoints; no credentials or telemetry in research records."""
import json
import io
import secrets
import uuid
from datetime import datetime, timezone

from flask import abort, flash, g, jsonify, redirect, render_template, request, session, send_file, url_for

from git_sync import SyncError, atomic_write
from monitoring import DEFAULT_SERVER, MonitorError, ServerMonitor
from training_metrics import MetricHistory, source_key


def register_monitoring_routes(app, directory, get_db):
    app.extensions['server_monitor'] = ServerMonitor()
    path = directory / 'monitor-server.json'
    with app.app_context():
        MetricHistory(get_db(), initialize=True)
        get_db().commit()

    def experiment(identifier):
        row = get_db().execute("SELECT * FROM entries WHERE id=? AND kind='experiment'", (identifier,)).fetchone()
        if not row:
            abort(404)
        result = dict(row)
        result['meta'] = json.loads(result['metadata'])
        return result

    def service():
        return app.extensions['server_monitor']

    def token():
        if 'monitor_token' not in session:
            session['monitor_token'] = secrets.token_urlsafe(32)
        # An invalidated owner session must not reuse earlier SSH credentials.
        return session['monitor_token'] + ':' + str(session.get('auth_version', ''))

    def release_workspace():
        if g.pop('workspace_locked', False):
            app.extensions['git_workspace'].lock.release()

    def settings():
        if path.is_file() and not path.is_symlink():
            try:
                value = json.loads(path.read_text())
                from monitoring import validate_server
                return validate_server(value)
            except (OSError, ValueError, MonitorError):
                pass
        return dict(DEFAULT_SERVER)

    @app.get('/monitor')
    def monitor_page():
        identifier = request.args.get('experiment', '')
        selected = experiment(identifier) if identifier else None
        runs = get_db().execute("SELECT id,title FROM entries WHERE kind='experiment' ORDER BY updated_at DESC LIMIT 200").fetchall()
        history = None
        if selected and selected['meta'].get('log_path'):
            key = source_key(service().state(token()) or settings(), selected['meta']['log_path'])
            history = MetricHistory(get_db()).read(identifier, key)
        return render_template('monitoring.html', active='monitor', server=settings(),
                               connected=service().state(token()), experiment=identifier, selected_run=selected,
                               runs=runs, history=history)

    @app.post('/api/monitor/connect')
    def monitor_connect():
        password = request.form.get('password', '')
        if password and request.remote_addr not in {'127.0.0.1', '::1'} and not request.is_secure:
            return jsonify(error='密码登录仅允许本机页面或 HTTPS。'), 400
        release_workspace()
        try:
            config = service().connect(token(), request.form, password)
            atomic_write(path, json.dumps(config, ensure_ascii=False).encode())
            return jsonify(server=config, connected=True)
        except MonitorError as error:
            return jsonify(error=str(error)), 400
        except OSError:
            service().disconnect(token())
            return jsonify(error='本机监控设置无法保存，请检查目录权限。'), 503

    @app.post('/api/monitor/server')
    def monitor_snapshot():
        if not service().state(token()):
            return jsonify(error='请先连接训练服务器。', connected=False), 401
        log_path = request.form.get('log_path', '')
        identifier = request.form.get('experiment', '')
        selected = experiment(identifier) if identifier else None
        if selected and not log_path:
            log_path = selected['meta'].get('log_path', '')
        release_workspace()
        try:
            snapshot = dict(service().snapshot(token(), log_path))
            if selected and log_path and log_path == selected['meta'].get('log_path', ''):
                key = source_key(snapshot['server'], log_path)
                history = MetricHistory(get_db()).record(identifier, key, snapshot.get('metrics', []),
                                                        reset=snapshot.get('history_reset', False))
                snapshot['history'] = history
            snapshot['experiment'] = {'id': identifier, 'total_steps': selected['meta'].get('total_steps', ''),
                                      'pid': selected['meta'].get('training_pid', '')} if selected else None
            return jsonify(snapshot)
        except MonitorError as error:
            connected = service().state(token()) is not None
            return jsonify(error=str(error), connected=connected), 400

    @app.post('/api/monitor/disconnect')
    def monitor_disconnect():
        service().disconnect(token())
        session.pop('monitor_token', None)
        return jsonify(connected=False)

    @app.get('/monitor/experiments/<identifier>/metrics.csv')
    def monitor_metrics_export(identifier):
        experiment(identifier)
        raw = MetricHistory(get_db()).export(identifier)
        return send_file(io.BytesIO(raw), as_attachment=True, download_name=f'training-metrics-{identifier[:8]}.csv', mimetype='text/csv')

    @app.post('/monitor/experiments/<identifier>/archive')
    def monitor_metrics_archive(identifier):
        run = experiment(identifier)
        if request.form.get('version') != str(run['version']):
            abort(409, '实验档案已更新，请刷新后再保存曲线附件。')
        db = get_db()
        if not db.execute('SELECT 1 FROM monitor_points WHERE experiment_id=? LIMIT 1', (identifier,)).fetchone():
            flash('还没有实际采集的指标，未创建空附件。', 'error')
            return redirect(url_for('monitor_page', experiment=identifier))
        raw = MetricHistory(db).export(identifier)
        storage = uuid.uuid4().hex
        target = directory / 'uploads' / storage
        stamp = datetime.now(timezone.utc).isoformat()
        name = f'training-curves-{datetime.now():%Y%m%d-%H%M%S}.csv'
        try:
            atomic_write(target, raw)
            db.execute('INSERT INTO attachments VALUES (?,?,?,?,?,?)',
                       (uuid.uuid4().hex, identifier, name, storage, len(raw), stamp))
            db.execute('UPDATE entries SET updated_at=?,version=version+1 WHERE id=?', (stamp, identifier))
            app.extensions['git_workspace'].write_entry(db, identifier)
            db.commit()
        except (OSError, SyncError):
            db.rollback()
            target.unlink(missing_ok=True)
            flash('曲线附件保存失败，请检查磁盘和资料同步状态。', 'error')
        else:
            flash('已保存真实指标 CSV 到实验附件；GitHub 同步后可在其他电脑查看。', 'success')
        return redirect(url_for('monitor_page', experiment=identifier))
