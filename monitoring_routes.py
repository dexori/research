"""Authenticated monitoring endpoints; no credentials or telemetry in research records."""
import json
import secrets

from flask import g, jsonify, render_template, request, session

from git_sync import atomic_write
from monitoring import DEFAULT_SERVER, MonitorError, ServerMonitor


def register_monitoring_routes(app, directory):
    app.extensions['server_monitor'] = ServerMonitor()
    path = directory / 'monitor-server.json'

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
        return render_template('monitoring.html', active='monitor', server=settings(),
                               connected=service().state(token()), experiment=request.args.get('experiment', ''))

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
        release_workspace()
        try:
            return jsonify(service().snapshot(token(), log_path))
        except MonitorError as error:
            connected = service().state(token()) is not None
            return jsonify(error=str(error), connected=connected), 400

    @app.post('/api/monitor/disconnect')
    def monitor_disconnect():
        service().disconnect(token())
        session.pop('monitor_token', None)
        return jsonify(connected=False)
