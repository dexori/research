"""Explicit training metrics, bounded incremental log reads and local history."""
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import re
import stat

MAX_READ = 2 * 1024 * 1024
MAX_POINTS = 10000
NUM = r'-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'


def parse_metrics(text):
    points = []
    for line in text.splitlines():
        line = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', line)
        step = re.search(r'''(?<![.\w])(?:global[_ ]step|step|steps|iteration|iter)["']?\s*[:=]\s*(\d[\d,]*(?:\.\d+)?)([kKmMbB]?)(?![\w.])''', line, re.I)
        if not step:
            continue
        if len(step[1]) > 24:
            continue
        multiplier = {'': 1, 'k': 1000, 'm': 1000000, 'b': 1000000000}[step[2].lower()]
        value = float(step[1].replace(',', '')) * multiplier
        if not value.is_integer() or not 0 <= value <= 10**12:
            continue
        point = {'step': int(value)}
        if step[2]:
            point['approximate'] = True
        for key, names in [('loss', r'(?:train[/_])?loss'), ('lr', r'(?:learning[_ /]rate|lr)'), ('epoch', r'(?:epoch|epch)')]:
            match = re.search(r'''\b(?:''' + names + r''')["']?\s*[:=]\s*(''' + NUM + ')', line, re.I)
            if match:
                value = float(match[1])
                if math.isfinite(value):
                    point[key] = value
        points.append(point)
    return points[-MAX_POINTS:]


class LogReader:
    """Read changes only. Detect truncation/rotation by size and a previous-byte anchor."""
    def __init__(self):
        self.offset = 0
        self.anchor = b''
        self.partial = b''
        self.tail = b''
        self.initial = True

    def read(self, client, path):
        reset = False
        with client.open_sftp() as sftp:
            sftp.get_channel().settimeout(8)
            with sftp.open(path, 'rb') as stream:
                info = stream.stat()
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('普通日志文件 required')
                if not self.initial:
                    if info.st_size < self.offset:
                        reset = True
                    elif self.anchor:
                        stream.seek(self.offset - len(self.anchor))
                        reset = stream.read(len(self.anchor)) != self.anchor
                if reset:
                    self.offset = 0; self.partial = b''; self.tail = b''
                missing = info.st_size - self.offset
                skipped = missing > MAX_READ
                start = max(self.offset, info.st_size - MAX_READ)
                stream.seek(start)
                raw = stream.read(min(MAX_READ, max(0, info.st_size - start)))
                self.offset = start + len(raw)
                if skipped:
                    self.partial = b''
                    if b'\n' in raw:
                        raw = raw.split(b'\n', 1)[1]
                data = self.partial + raw
                complete, sep, partial = data.rpartition(b'\n')
                self.partial = partial[-65536:] if sep else data[-65536:]
                # Never parse a half-written numeric value as a completed metric.
                points = parse_metrics(complete.decode('utf-8', 'replace')) if sep else []
                if self.initial or reset:
                    # On reconnect, do not replay earlier runs appended to the same file.
                    boundaries = [i for i in range(1, len(points)) if points[i]['step'] < points[i - 1]['step']]
                    if boundaries:
                        points = points[boundaries[-1]:]
                self.tail = (self.tail + raw)[-65536:]
                stream.seek(max(0, self.offset - 64))
                self.anchor = stream.read(min(64, self.offset))
        self.initial = False
        text = self.tail.decode('utf-8', 'replace')
        display_truncated = self.offset > len(self.tail)
        if display_truncated and '\n' in text:
            text = text.split('\n', 1)[1]
        lines = text.splitlines()
        return {'log': '\n'.join(lines[-250:]), 'metrics': points,
                'reset': reset, 'truncated': display_truncated or len(lines) > 250,
                'history_limited': skipped}


def source_key(server, path):
    identity = [server['host'], server['port'], server['user'], path]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


class MetricHistory:
    def __init__(self, db, initialize=False):
        self.db = db
        if not initialize:
            return
        db.executescript('''
            CREATE TABLE IF NOT EXISTS monitor_series (
                experiment_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
                source TEXT NOT NULL, segment INTEGER NOT NULL DEFAULT 0,
                latest_step INTEGER, updated_at TEXT NOT NULL,
                PRIMARY KEY(experiment_id,source)
            );
            CREATE TABLE IF NOT EXISTS monitor_points (
                experiment_id TEXT NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
                source TEXT NOT NULL, segment INTEGER NOT NULL, step INTEGER NOT NULL,
                loss REAL, lr REAL, epoch REAL, observed_at TEXT NOT NULL, approximate INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(experiment_id,source,segment,step)
            );
        ''')
        columns = {row[1] for row in db.execute('PRAGMA table_info(monitor_points)')}
        if 'approximate' not in columns:
            db.execute('ALTER TABLE monitor_points ADD COLUMN approximate INTEGER NOT NULL DEFAULT 0')

    def record(self, identifier, source, points, reset=False):
        if not points and not reset:
            return self.read(identifier, source)
        stamp = datetime.now(timezone.utc).isoformat()
        existing = self.db.execute('SELECT * FROM monitor_series WHERE experiment_id=? AND source=?', (identifier, source)).fetchone()
        segment = existing['segment'] if existing else 0
        # Reconnecting replays the tail; only the latest incoming step detects a rewind.
        if existing and (reset or points and existing['latest_step'] is not None and points[-1]['step'] < existing['latest_step']):
            segment += 1
        for i, point in enumerate(points):
            if i and point['step'] < points[i - 1]['step']:
                segment += 1
            self.db.execute('''INSERT INTO monitor_points (experiment_id,source,segment,step,loss,lr,epoch,observed_at,approximate) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(experiment_id,source,segment,step) DO UPDATE SET
                    loss=COALESCE(excluded.loss,loss), lr=COALESCE(excluded.lr,lr), epoch=COALESCE(excluded.epoch,epoch), approximate=excluded.approximate''',
                (identifier, source, segment, point['step'], point.get('loss'), point.get('lr'), point.get('epoch'), stamp, bool(point.get('approximate'))))
        latest = points[-1]['step'] if points else None if reset else existing['latest_step'] if existing else None
        self.db.execute('''INSERT INTO monitor_series VALUES (?,?,?,?,?) ON CONFLICT(experiment_id,source)
            DO UPDATE SET segment=excluded.segment,latest_step=excluded.latest_step,updated_at=excluded.updated_at''',
            (identifier, source, segment, latest, stamp))
        # Bound disk growth; curves are collected history, never claimed complete.
        self.db.execute('''DELETE FROM monitor_points WHERE experiment_id=? AND source=? AND rowid NOT IN
            (SELECT rowid FROM monitor_points WHERE experiment_id=? AND source=? ORDER BY segment DESC,step DESC LIMIT ?)''',
            (identifier, source, identifier, source, MAX_POINTS))
        self.db.commit()
        return self.read(identifier, source)

    def read(self, identifier, source):
        series = self.db.execute('SELECT * FROM monitor_series WHERE experiment_id=? AND source=?', (identifier, source)).fetchone()
        segment = series['segment'] if series else 0
        rows = self.db.execute('''SELECT step,loss,lr,epoch,observed_at,approximate FROM monitor_points
            WHERE experiment_id=? AND source=? AND segment=? ORDER BY step DESC LIMIT 2000''', (identifier, source, segment)).fetchall()
        return {'points': [dict(row) for row in reversed(rows)], 'segment': segment,
                'latest_step': series['latest_step'] if series else None,
                'updated_at': series['updated_at'] if series else None}

    def export(self, identifier):
        output = io.StringIO(); writer = csv.writer(output)
        writer.writerow(['source', 'segment', 'step', 'loss', 'learning_rate', 'epoch', 'observed_at', 'approximate_step'])
        rows = self.db.execute('''SELECT source,segment,step,loss,lr,epoch,observed_at,approximate FROM monitor_points
            WHERE experiment_id=? ORDER BY source,segment,step''', (identifier,)).fetchall()
        writer.writerows(tuple(row) for row in rows)
        return output.getvalue().encode('utf-8-sig')
