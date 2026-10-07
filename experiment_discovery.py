"""Validate a read-only probe result and create an unsaved, evidence-backed draft."""
from datetime import datetime, timezone
import hashlib
import json
import re

from monitoring import MonitorError, validate_log_path

FIELDS = {'model', 'dataset', 'seed', 'learning_rate', 'batch_size', 'total_steps', 'epochs',
          'output_dir', 'run_name', 'log_path', 'code_version', 'gpu_ids'}


def normalize_run(run, server):
    if not isinstance(run, dict) or not re.fullmatch(r'[a-f0-9]{64}', str(run.get('identity', ''))):
        raise MonitorError('训练身份信息无效，未创建档案。')
    pid = str(run.get('pid', ''))
    if not pid.isdigit() or not 0 < int(pid) <= 2147483647:
        raise MonitorError('训练 PID 无效。')
    raw_fields = run.get('fields')
    if not isinstance(raw_fields, dict):
        raise MonitorError('训练参数格式无效。')
    fields, sources = {}, {}
    raw_sources = run.get('sources', {})
    if not isinstance(raw_sources, dict):
        raw_sources = {}
    for key in FIELDS:
        raw_value = raw_fields.get(key, '')
        if not isinstance(raw_value, (str, int, float)):
            continue
        value = str(raw_value).strip()
        if not value or len(value) > 1024 or any(ord(c) < 32 for c in value):
            continue
        if re.search(r'(?:password|token|api.?key|secret)\s*[:=]|://[^/\s]*@', value, re.I):
            continue
        if key in {'batch_size', 'total_steps'} and (not value.isdigit() or not 0 < int(value) <= 10**12):
            continue
        if key in {'learning_rate', 'epochs'}:
            try:
                number = float(value)
                if not 0 < number < float('inf'):
                    continue
            except ValueError:
                continue
        if key == 'log_path':
            try:
                value = validate_log_path(value)
            except MonitorError:
                continue
        fields[key] = value
        sources[key] = str(raw_sources.get(key, '自动读取（待核对）'))[:1400]
    identity = hashlib.sha256(json.dumps([server['host'],server['port'],server['user'],run['identity']]).encode()).hexdigest()
    extras = run.get('extras', {})
    if not isinstance(extras, dict):
        extras = {}
    clean_extras = {}
    for key, value in list(extras.items())[:20]:
        if (not isinstance(key, str) or len(key) > 100 or re.search(r'secret|password|token|key|credential', key, re.I)):
            continue
        value = str(value)
        if re.fullmatch(r'-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|true|false', value, re.I):
            clean_extras[key] = value[:100]
    script = str(run.get('script', 'training'))[:120]
    title = fields.get('run_name') or ' · '.join(v for v in [fields.get('model'), fields.get('dataset'), script] if v)
    try:
        workers = max(1, min(1000, int(run.get('worker_count', 1))))
    except (TypeError, ValueError, OverflowError):
        workers = 1
    return {'pid': pid, 'identity': identity, 'title': title[:240], 'fields': fields,
            'sources': sources, 'extras': clean_extras, 'worker_count': workers,
            'script': script, 'cwd': str(run.get('cwd', ''))[:1024]}


def draft_values(run, server):
    stamp = datetime.now(timezone.utc).isoformat()
    fields = run['fields']
    params = {**{k: v for k, v in fields.items() if k not in {'log_path', 'run_name'}}, **run['extras']}
    meta = {k: v for k, v in fields.items() if k in FIELDS - {'run_name', 'output_dir'}}
    meta.update(training_pid=run['pid'], params=json.dumps(params, ensure_ascii=False, indent=2),
                discovery_identity=run['identity'], discovery_server=f"{server['user']}@{server['host']}:{server['port']}",
                discovery_sources=json.dumps(run['sources'], ensure_ascii=False), discovery_time=stamp)
    evidence = '\n'.join(f'- `{key}`：{source}' for key, source in run['sources'].items())
    body = ('## 实验目的\n\n待补充：本次实验要检验的假设与对照。平台没有从参数推断科研结论。\n\n'
            f"## 自动读取的训练现场\n\n- 识别时间：{stamp}\n- 主进程 PID：{run['pid']}\n"
            f"- 已识别进程数：{run['worker_count']}\n- 脚本：`{run['script']}`\n- 工作目录：`{run['cwd']}`\n\n"
            '## 参数来源\n\n' + (evidence or '未识别到可确认的配置参数。') +
            '\n\n配置文件反映读取时内容；命令行覆盖优先。无法识别的字段留空，不执行训练脚本或猜默认值。\n\n'
            '## 观察与结果\n\n待记录真实指标。\n\n## 结论与下一步\n\n待研究完成后补充。\n')
    return meta, body
