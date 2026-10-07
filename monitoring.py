"""Bounded, read-only SSH telemetry. Credentials live in memory, never in Git."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import io
import math
from pathlib import PurePosixPath
import re
import secrets
import shlex
import socket
import stat
import threading
import time
from training_metrics import LogReader, parse_metrics

import paramiko

DEFAULT_SERVER = {'name': '训练服务器', 'host': '106.54.52.53', 'port': 2222, 'user': 'zdx'}
MAX_OUTPUT = 64 * 1024
TTL = 8 * 3600
GPU_COMMAND = ('nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,'
               'temperature.gpu,power.draw --format=csv,noheader,nounits')


class MonitorError(Exception):
    pass


def validate_server(fields):
    host = str(fields.get('host', '')).strip()
    user = str(fields.get('user', '')).strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]{0,252}', host):
        raise MonitorError('请填写有效服务器地址。')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,63}', user):
        raise MonitorError('请填写有效 SSH 用户名。')
    try:
        port = int(fields.get('port', 22))
        if not 1 <= port <= 65535:
            raise ValueError()
    except (TypeError, ValueError):
        raise MonitorError('SSH 端口应为 1–65535。') from None
    return {'name': str(fields.get('name', '训练服务器'))[:80], 'host': host, 'port': port, 'user': user}


def validate_log_path(value):
    path = str(value).strip()
    if not path:
        return ''
    p = PurePosixPath(path)
    if (len(path) > 1024 or not p.is_absolute() or '..' in p.parts
            or any(ord(c) < 32 for c in path) or p.parts[1:2] in [('proc',), ('sys',), ('dev',)]
            or p.suffix.lower() not in {'.log', '.txt', '.out', '.err', '.jsonl', '.csv'}):
        raise MonitorError('请填写 .log/.txt/.out/.err/.jsonl/.csv 日志的绝对路径。')
    return path


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def parse_gpus(text):
    result = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) != 7:
            continue
        result.append(dict(zip(('index', 'name', 'utilization', 'used', 'total', 'temperature', 'power'),
                               [row[0].strip(), row[1].strip()] + [number(v.strip()) for v in row[2:]])))
    return result[:32]


class ServerMonitor:
    def __init__(self, client_factory=None, clock=time.monotonic):
        self.client_factory = client_factory or paramiko.SSHClient
        self.clock = clock
        self._sessions = {}
        self._lock = threading.RLock()

    def _client(self, config, password):
        client = self.client_factory()
        try:
            client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            client.connect(config['host'], port=config['port'], username=config['user'],
                           password=password or None, timeout=8, banner_timeout=8, auth_timeout=8,
                           channel_timeout=8, allow_agent=not bool(password), look_for_keys=not bool(password))
            return client
        except Exception:
            client.close()
            raise

    @staticmethod
    def _error(error):
        if isinstance(error, paramiko.BadHostKeyException):
            return MonitorError('SSH 主机密钥发生变化，已拒绝连接。请核对服务器后更新本机 known_hosts。')
        if isinstance(error, paramiko.AuthenticationException):
            return MonitorError('SSH 登录失败，请检查用户名、密码或本机密钥。')
        if isinstance(error, (socket.timeout, TimeoutError)):
            return MonitorError('服务器连接或查询超时，请检查网络后重试。')
        if isinstance(error, paramiko.SSHException):
            return MonitorError('SSH 连接失败；首次使用请在本机终端连接一次，核对并信任主机指纹。')
        return MonitorError('服务器暂时无法读取，请检查网络、SSH 端口和访问权限。')

    def connect(self, token, fields, password=''):
        config = validate_server(fields)
        if not isinstance(password, str) or len(password) > 1024:
            raise MonitorError('密码格式不正确。')
        try:
            client = self._client(config, password)
            transport = client.get_transport()
            if transport:
                transport.set_keepalive(20)
        except Exception as error:
            raise self._error(error) from None
        with self._lock:
            self._prune()
            if len(self._sessions) >= 8 and token not in self._sessions:
                client.close()
                raise MonitorError('监控会话较多，请断开其他页面后重试。')
            self.disconnect(token)
            nonce = secrets.token_hex(16)
            timer = threading.Timer(TTL, self._expire, args=(token, nonce))
            timer.daemon = True
            self._sessions[token] = {'config': config, 'password': password,
                                     'expires': self.clock() + TTL, 'lock': threading.Lock(),
                                     'nonce': nonce, 'timer': timer, 'client': client,
                                     'cache': {}, 'log_readers': {}}
            timer.start()
        return config

    def _expire(self, token, nonce):
        with self._lock:
            if self._sessions.get(token, {}).get('nonce') == nonce:
                self.disconnect(token)

    def _prune(self):
        for key in list(self._sessions):
            if self._sessions[key]['expires'] <= self.clock():
                self.disconnect(key)

    def state(self, token):
        with self._lock:
            self._prune()
            item = self._sessions.get(token)
            return dict(item['config']) if item else None

    def disconnect(self, token):
        with self._lock:
            item = self._sessions.pop(token, None)
            if item:
                item['timer'].cancel()
                item['client'].close()

    def _command(self, client, command):
        stdin, stdout, stderr = client.exec_command(command, timeout=8)
        stdin.close()
        channel = stdout.channel
        deadline = self.clock() + 8
        output, errors = bytearray(), bytearray()
        try:
            while True:
                if channel.recv_ready():
                    output.extend(channel.recv(min(8192, MAX_OUTPUT - len(output) + 1)))
                if channel.recv_stderr_ready():
                    errors.extend(channel.recv_stderr(min(8192, MAX_OUTPUT - len(errors) + 1)))
                if len(output) > MAX_OUTPUT or len(errors) > MAX_OUTPUT:
                    raise MonitorError('服务器输出过大，已停止本次读取。')
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if self.clock() >= deadline:
                    raise TimeoutError()
                time.sleep(.02)
            status = channel.recv_exit_status()
            return output.decode('utf-8', 'replace'), status
        finally:
            channel.close()

    @staticmethod
    def _log(client, path):
        with client.open_sftp() as sftp:
            sftp.get_channel().settimeout(8)
            with sftp.open(path, 'rb') as stream:
                info = stream.stat()
                if not stat.S_ISREG(info.st_mode):
                    raise MonitorError('只支持读取普通日志文件。')
                start = max(0, info.st_size - MAX_OUTPUT)
                stream.seek(start)
                raw = stream.read(MAX_OUTPUT)
        text = raw.decode('utf-8', 'replace')
        if start and '\n' in text:
            text = text.split('\n', 1)[1]
        lines = text.splitlines()
        return '\n'.join(lines[-250:]), start > 0 or len(lines) > 250

    def snapshot(self, token, log_path=''):
        path = validate_log_path(log_path)
        with self._lock:
            self._prune()
            item = self._sessions.get(token)
        if not item:
            raise MonitorError('监控未连接或已过期，请重新登录服务器。')
        if not item['lock'].acquire(blocking=False):
            raise MonitorError('正在读取服务器，请稍后刷新。')
        try:
            started = self.clock()
            client = item['client']
            transport = client.get_transport()
            if not transport or not transport.is_active():
                client.close()
                client = self._client(item['config'], item['password'])
                item['client'] = client
                transport = client.get_transport()
                if transport:
                    transport.set_keepalive(20)
            warnings = []
            gpu_text, gpu_status = self._command(client, GPU_COMMAND)
            if gpu_status:
                warnings.append('GPU 查询不可用，请检查驱动或 nvidia-smi。')
            command = 'ps -u ' + shlex.quote(item['config']['user']) + ' -o pid=,etimes=,pcpu=,pmem=,comm=,args= --sort=-pcpu'
            cache = item['cache']
            if 'ps' not in cache or self.clock() - cache['ps_at'] >= 5:
                cache['ps'] = self._command(client, command)
                cache['ps_at'] = self.clock()
            ps_text, ps_status = cache['ps']
            processes = []
            for line in ps_text.splitlines():
                values = line.strip().split(None, 5)
                if len(values) == 6 and re.search(r'python|torchrun|accelerate|deepspeed', values[4] + ' ' + values[5], re.I):
                    processes.append({'pid': values[0], 'seconds': number(values[1]), 'cpu': number(values[2]),
                                      'memory': number(values[3]), 'command': values[5][:1200]})
            if ps_status:
                warnings.append('用户进程查询不可用。')
            if 'uptime' not in cache or self.clock() - cache['uptime_at'] >= 30:
                cache['uptime'] = self._command(client, 'uptime')[0]
                cache['uptime_at'] = self.clock()
            uptime = cache['uptime']
            log, truncated = '', False
            points, reset, limited = [], False, False
            if path:
                try:
                    if path not in item['log_readers']:
                        if len(item['log_readers']) >= 8:
                            item['log_readers'].pop(next(iter(item['log_readers'])))
                        item['log_readers'][path] = LogReader()
                    reading = item['log_readers'][path].read(client, path)
                    log, truncated, points = reading['log'], reading['truncated'], reading['metrics']
                    reset, limited = reading['reset'], reading['history_limited']
                    if limited:
                        warnings.append('日志较大，本次仅回读末尾 2 MB；更早的曲线未采集。')
                except (OSError, ValueError, MonitorError):
                    warnings.append('日志无法读取，请检查路径、文件类型和权限。')
            return {'server': dict(item['config']), 'updated_at': datetime.now(timezone.utc).isoformat(),
                    'gpus': parse_gpus(gpu_text) if not gpu_status else [], 'processes': processes[:80],
                    'uptime': uptime.strip()[:500], 'log': log, 'log_path': path,
                    'truncated': truncated, 'metrics': points, 'history_reset': reset,
                    'history_limited': limited, 'warnings': warnings,
                    'latency_ms': round((self.clock() - started) * 1000),
                    'process_age_seconds': round(self.clock() - cache['ps_at'], 1)}
        except MonitorError:
            raise
        except Exception as error:
            item['client'].close()
            raise self._error(error) from None
        finally:
            item['lock'].release()
