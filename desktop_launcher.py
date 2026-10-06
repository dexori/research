"""Linux desktop launcher: reuse a live workspace or start one in the background."""
from __future__ import annotations

import argparse
import fcntl
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from git_sync import atomic_write

ROOT = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:8765'
MARKER = 'X-ResearchHub-Launcher=true'


class LaunchError(Exception):
    pass


def probe(port=8765):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
    try:
        connection.request('GET', '/login')
        response = connection.getresponse()
        return 'ready' if response.getheader('X-Research-Hub') == '1' else 'occupied'
    except ConnectionRefusedError:
        return 'stopped'
    except (OSError, http.client.HTTPException):
        return 'occupied'
    finally:
        connection.close()


def launch_environment(directory):
    environment = os.environ.copy()
    path = directory / 'desktop-launcher.json'
    if path.is_file() and not path.is_symlink():
        try:
            config = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise LaunchError('启动设置无法读取，请重新安装桌面图标。') from None
        for name in ['RESEARCH_CODEX_BIN', 'RESEARCH_CODEX_HOME']:
            if name not in environment and isinstance(config.get(name), str):
                environment[name] = config[name]
    return environment


def ensure_running(root=ROOT, directory=None, port=8765, timeout=20):
    root = Path(root).resolve()
    directory = Path(directory or root / 'data').resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = os.open(directory / 'desktop-launcher.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'a') as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LaunchError('平台正在启动，请稍后再双击图标。')
                time.sleep(0.2)
        state = probe(port)
        if state == 'ready':
            return False
        if state == 'occupied':
            raise LaunchError(f'本机 {port} 端口被其他服务占用，未重复启动平台。')
        registry = directory / 'desktop-server.json'
        if registry.is_file() and not registry.is_symlink():
            try:
                saved = json.loads(registry.read_text())
                pid = saved['pid']
                if not isinstance(pid, int) or pid <= 0:
                    raise ValueError()
                command = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
                expected = [str(root / '.venv/bin/python').encode(), str(root / 'app.py').encode(),
                            b'--host', b'127.0.0.1', b'--port', str(port).encode(), b'']
                if command == expected:
                    while time.monotonic() < deadline:
                        if probe(port) == 'ready':
                            return False
                        time.sleep(0.2)
                    raise LaunchError('平台仍在初始化，未重复启动。请稍后再双击图标。')
            except (OSError, ValueError, KeyError, TypeError):
                pass
        python = root / '.venv/bin/python'
        if not python.is_file():
            raise LaunchError('尚未安装平台依赖，请按 README 创建 .venv 并安装 requirements.txt。')
        environment = launch_environment(directory)
        environment['RESEARCH_DATA_DIR'] = str(directory)
        log_fd = os.open(directory / 'desktop-server.log', os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(log_fd, 'ab', buffering=0) as log:
            process = subprocess.Popen([str(python), str(root / 'app.py'), '--host', '127.0.0.1', '--port', str(port)],
                                       cwd=root, env=environment, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        atomic_write(registry, json.dumps({'pid': process.pid}).encode('utf-8'))
        while time.monotonic() < deadline:
            state = probe(port)
            if state == 'ready':
                return True
            if process.poll() is not None:
                raise LaunchError(f'平台启动未完成，请查看 {directory / "desktop-server.log"}。')
            if state == 'occupied':
                time.sleep(0.3)
                continue
            time.sleep(0.2)
        # Never stop an existing process or discard the user's data on a timeout.
        raise LaunchError('平台仍在启动，请稍后打开 http://127.0.0.1:8765；启动日志保存在 data/desktop-server.log。')


def desktop_quote(value):
    value = str(value)
    if any(char in value for char in ['\n', '\r', '\x00']):
        raise LaunchError('项目路径包含无效字符。')
    # Desktop Entry escaping, then Exec quoting (not shell quoting).
    value = value.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$')
    return '"' + value.replace('\\', '\\\\').replace('%', '%%') + '"'


def desktop_entry(root):
    root = Path(root).resolve()
    return '\n'.join([
        '[Desktop Entry]', 'Version=1.0', 'Type=Application', 'Name=研序科研工作台',
        'Name[zh_CN]=研序科研工作台', 'Comment=打开文献、实验、科研助手与 GitHub 同步',
        'Exec=' + desktop_quote(root / '.venv/bin/python') + ' ' + desktop_quote(root / 'desktop_launcher.py'),
        'Icon=' + str(root / 'static/favicon.svg'), 'Terminal=false', 'StartupNotify=false',
        'Categories=Education;', 'Keywords=Research;Papers;科研;文献;', MARKER, '',
    ])


def install(root=ROOT):
    root = Path(root).resolve()
    desktop_tool = shutil.which('xdg-user-dir')
    desktop = Path.home() / 'Desktop'
    if desktop_tool:
        result = subprocess.run([desktop_tool, 'DESKTOP'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            desktop = Path(result.stdout.strip())
    if desktop.resolve() == Path.home().resolve() or not desktop.is_absolute():
        raise LaunchError('系统未启用桌面目录，请先在文件管理器启用桌面。')
    applications = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'applications'
    targets = [desktop / 'research-hub.desktop', applications / 'research-hub.desktop']
    for target in targets:
        if target.is_symlink() or (target.exists() and MARKER not in target.read_text(encoding='utf-8')):
            raise LaunchError(f'已有其他文件占用 {target}，未覆盖，请先检查。')
    entry = desktop_entry(root).encode('utf-8')
    for target in targets:
        atomic_write(target, entry)
        target.chmod(0o755)
    # Persist paths only, not credentials or provider settings. Desktop sessions
    # may have a shorter PATH than the terminal used to install the launcher.
    config = {name: os.environ[name] for name in ['RESEARCH_CODEX_BIN', 'RESEARCH_CODEX_HOME'] if os.environ.get(name)}
    binary = config.get('RESEARCH_CODEX_BIN') or shutil.which('codex')
    if binary:
        config['RESEARCH_CODEX_BIN'] = str(Path(binary).resolve())
    (root / 'data').mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write(root / 'data/desktop-launcher.json', json.dumps(config).encode('utf-8'))
    gio = shutil.which('gio')
    if gio:
        subprocess.run([gio, 'set', str(targets[0]), 'metadata::trusted', 'true'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    print('桌面与应用菜单图标已安装：' + str(targets[0]))
    return targets


def main():
    parser = argparse.ArgumentParser(description='研序科研工作台桌面启动器（Linux）')
    parser.add_argument('--install', action='store_true', help='安装桌面与应用菜单图标')
    parser.add_argument('--no-browser', action='store_true', help='只启动/检查平台，不打开浏览器')
    args = parser.parse_args()
    try:
        if args.install:
            install()
        else:
            started = ensure_running()
            print('平台已启动。' if started else '平台已经运行，直接打开网页。')
            if not args.no_browser:
                opener = shutil.which('xdg-open')
                if not opener:
                    raise LaunchError('未找到浏览器启动工具，请手动打开 ' + URL)
                subprocess.Popen([opener, URL], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        return 0
    except (LaunchError, OSError, subprocess.SubprocessError) as error:
        message = str(error) if isinstance(error, LaunchError) else '启动遇到系统错误，请检查项目路径和启动日志。'
        print(message, file=sys.stderr)
        notify = shutil.which('notify-send')
        if notify:
            subprocess.run([notify, '研序科研工作台', message], timeout=5, check=False)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
