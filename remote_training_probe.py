"""Dependency-free, read-only probe executed with python3 -I on the SSH server.

Only the current UID's training processes and their explicit config/output paths
are inspected. Never executes a training script, config, shell snippet or Git.
Only allowlisted settings leave the process; secrets/raw environment never do.
"""
import hashlib
import ast
import json
import math
import os
from pathlib import Path
import re
import stat
import sys

ALIASES = {
    'model': ('policy.type', 'model.type', 'model.name', 'model_name', 'model_name_or_path', 'model', 'policy.pretrained_path'),
    'dataset': ('dataset.repo_id', 'dataset.name', 'dataset_id', 'dataset_name', 'data.dataset_name', 'dataset', 'dataset_path', 'dataset_root'),
    'seed': ('seed', 'training.seed', 'train.seed'),
    'learning_rate': ('optimizer.lr', 'policy.optimizer_lr', 'learning_rate', 'lr', 'training.learning_rate', 'train.lr'),
    'batch_size': ('batch_size', 'training.batch_size', 'train.batch_size', 'per_device_train_batch_size'),
    'total_steps': ('stop_after_steps', 'steps', 'total_steps', 'max_steps', 'num_train_steps', 'training.steps', 'train.steps'),
    'epochs': ('epochs', 'num_train_epochs', 'training.epochs'),
    'output_dir': ('output_dir', 'output', 'logging_dir', 'save_dir', 'training.output_dir'),
    'run_name': ('job_name', 'run_name', 'exp_name', 'experiment_name', 'variant'),
    'log_path': ('log_path', 'log_file', 'logfile', 'training.log_path'),
    'code_version': ('code_version', 'git_commit'),
}
CONFIG_KEYS = ('config_path', 'config', 'config_file', 'cfg')
SECRET = re.compile(r'password|secret|token|api.?key|credential|authorization|cookie|private.?key', re.I)
MAX_FILE = 256 * 1024


def text(value, limit=600):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if not isinstance(value, (str, int, float)):
        return ''
    if isinstance(value, float) and not math.isfinite(value):
        return ''
    result = str(value).strip()
    if len(result) > limit or any(ord(c) < 32 for c in result):
        return ''
    if re.search(r'(?:password|token|api.?key|secret)\s*[:=]|://[^/\s]*@', result, re.I):
        return ''
    return result


def flags(argv):
    result = {}
    for i, arg in enumerate(argv):
        if not arg.startswith('--'):
            continue
        key, sep, value = arg[2:].partition('=')
        key = key.replace('-', '_').lower()
        if not sep:
            value = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith('--') else 'true'
        if not SECRET.search(key):
            result[key] = text(value)
    return result


def flatten(data, prefix='', depth=0):
    result = {}
    if not isinstance(data, dict) or depth > 8:
        return result
    for key, value in list(data.items())[:1000]:
        if not isinstance(key, str) or SECRET.search(key):
            continue
        name = (prefix + '.' + key if prefix else key).replace('-', '_').lower()
        if isinstance(value, dict):
            result.update(flatten(value, name, depth + 1))
        else:
            clean = text(value)
            if clean:
                result[name] = clean
    return result


def yaml_values(raw):
    # Conservative simple scalar mappings only. No tags, anchors, includes or eval.
    result, stack = {}, []
    for line in raw.splitlines():
        match = re.match(r'^( *)([A-Za-z_][\w.-]*)\s*:\s*(.*?)\s*$', line)
        if not match:
            continue
        indent, key, value = len(match[1]), match[2], match[3]
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if not value:
            stack.append((indent, key)); continue
        if value.startswith(('!', '&', '*', '|', '>', '{', '[')) or '${' in value:
            continue
        value = value.split(' #', 1)[0].strip().strip('"\'')
        name = '.'.join([k for _, k in stack] + [key]).replace('-', '_').lower()
        if not SECRET.search(name):
            result[name] = text(value)
    return result


def read_config(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
                return {}
            raw = stream.read(MAX_FILE + 1).decode('utf-8')
        if path.suffix.lower() == '.json':
            return flatten(json.loads(raw))
        if path.suffix.lower() in {'.yaml', '.yml'}:
            return yaml_values(raw)
    except (OSError, ValueError, UnicodeError, RecursionError):
        pass
    return {}


def resolve(value, cwd):
    if not value or '://' in value or any(ord(c) < 32 for c in value):
        return None
    path = Path(value)
    return path if path.is_absolute() else cwd / path


def training_command(argv):
    if not argv:
        return False
    executable = Path(argv[0]).name
    if executable in {'torchrun', 'accelerate', 'deepspeed'}:
        return True
    for arg in argv[1:]:
        if arg == '-c':
            return False
        if arg.startswith('-'):
            continue
        name = Path(arg).name
        return bool(re.search(r'(?:^|[_.-])(?:train|finetune|pretrain|sft)(?:[_.-]|$)', name, re.I)
                         or 'torch.distributed' in arg or 'accelerate.commands.launch' in arg)
    return False


def process(pid):
    root = Path('/proc') / str(pid)
    try:
        if root.stat().st_uid != os.getuid():
            return None
        with (root / 'cmdline').open('rb') as stream:
            raw = stream.read(32768)
        argv = [v.decode('utf-8', 'replace') for v in raw.split(b'\0') if v]
        if not training_command(argv):
            return None
        fields = (root / 'stat').read_text().rsplit(')', 1)[1].split()
        cwd = Path(os.readlink(root / 'cwd'))
        return {'pid': pid, 'parent': int(fields[1]), 'start': fields[19], 'cwd': cwd, 'argv': argv}
    except (OSError, ValueError, IndexError):
        return None


def regular_log(path):
    if path is None or path.suffix.lower() not in {'.log', '.txt', '.out', '.err', '.jsonl', '.csv'}:
        return ''
    try:
        if path.is_file() and len(str(path)) <= 1024:
            return str(path)
    except OSError:
        pass
    return ''


def startup_config(path):
    """Read a bounded, structured LeRobot-style startup config, never eval code.

    Only a complete dict containing policy + batch_size + steps/optimizer qualifies;
    arbitrary log lines, metric dicts, and unstructured/default values do not.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return {}
            raw = stream.read(128 * 1024).decode('utf-8', 'replace')
        # Logging may prefix the first line with timestamp/source. Never run it.
        starts = [match.start() for match in re.finditer(r'\{\s*[\x27"](?:accelerator|batch_size|dataset|policy)[\x27"]\s*:', raw)][:8]
        for start in starts:
            depth, quote, escape = 0, '', False
            for end in range(start, len(raw)):
                char = raw[end]
                if quote:
                    if escape:
                        escape = False
                    elif char == '\\':
                        escape = True
                    elif char == quote:
                        quote = ''
                    continue
                if char in ('"', "'"):
                    quote = char
                elif char == '{':
                    depth += 1
                elif char == '}':
                    depth -= 1
                    if depth == 0:
                        snippet = raw[start:end + 1]
                        try:
                            data = json.loads(snippet)
                        except ValueError:
                            tree = ast.parse(normalize_enum_repr(snippet), mode='eval')
                            if sum(1 for _ in ast.walk(tree)) > 30000:
                                break
                            data = literal_settings(tree.body)
                        if (isinstance(data, dict) and isinstance(data.get('policy'), dict)
                                and 'batch_size' in data and ('steps' in data or 'optimizer' in data)):
                            return flatten(data)
                        break
    except (OSError, ValueError, SyntaxError, RecursionError, MemoryError):
        pass
    return {}


def normalize_enum_repr(raw):
    """Convert a pprint enum's literal value outside strings, without imports."""
    enum = re.compile(r'''<[A-Za-z_][\w.]*:\s*('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|-?\d+)>''')
    result, quote, escape, index = [], '', False, 0
    while index < len(raw):
        char = raw[index]
        if not quote and char == '<':
            match = enum.match(raw, index)
            if match:
                result.append(match[1]); index = match.end(); continue
        result.append(char)
        if quote:
            if escape:
                escape = False
            elif char == '\\':
                escape = True
            elif char == quote:
                quote = ''
        elif char in ('"', "'"):
            quote = char
        index += 1
    return ''.join(result)


def literal_settings(node, depth=0):
    """Extract literal config values; drop code/objects without evaluating them.

    pprint(dataclass) may contain PosixPath(...) values. Only that exact simple
    path representation is converted to text; no functions are ever invoked.
    """
    if depth > 8:
        return None
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, (str, int, float, bool)) else None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)) and isinstance(node.operand, ast.Constant):
        value = node.operand.value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.Dict):
        result = {}
        for key, value in list(zip(node.keys, node.values))[:1000]:
            if isinstance(key, ast.Constant) and isinstance(key.value, str) and not SECRET.search(key.value):
                clean = literal_settings(value, depth + 1)
                if clean is not None:
                    result[key.value] = clean
        return result
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in {'Path', 'PosixPath', 'PurePosixPath'}
            and len(node.args) == 1 and not node.keywords
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
        return node.args[0].value
    return None


def inspect_run(proc, members, boot):
    cwd, cli = proc['cwd'], flags(proc['argv'])
    configs, seen = [], set()
    for key in CONFIG_KEYS:
        path = resolve(cli.get(key, ''), cwd)
        if path:
            configs.append(path)
    output = next((cli[k] for k in ALIASES['output_dir'] if cli.get(k)), '')
    output_path = resolve(output, cwd)
    if output_path:
        for name in ['train_config.json', 'config.json', 'args.json', 'config.yaml', '.hydra/config.yaml',
                     'checkpoints/last/pretrained_model/train_config.json']:
            configs.append(output_path / name)
    found, sources, extras = {}, {}, {}
    # Explicit CLI config takes precedence over generic output configs.
    for path in configs[:8]:
        if str(path) in seen:
            continue
        seen.add(str(path)); values = read_config(path)
        for field, aliases in ALIASES.items():
            value = next((values[k] for k in aliases if values.get(k)), '')
            if value and field not in found:
                found[field] = value; sources[field] = '配置：' + str(path)
        for key, value in values.items():
            if re.search(r'(?:lambda|weight|horizon|window|contrastive|dtw|hidden_dim|num_layers|grad_accum)', key) and not SECRET.search(key):
                if re.fullmatch(r'-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|true|false', value, re.I):
                    extras.setdefault(key, value)
    for field, aliases in ALIASES.items():
        value = next((cli[k] for k in aliases if cli.get(k)), '')
        if value:
            found[field] = value; sources[field] = '启动参数'
    for key, value in cli.items():
        if re.search(r'(?:lambda|weight|horizon|window|contrastive|dtw|hidden_dim|num_layers|grad_accum)', key) and not SECRET.search(key):
            if re.fullmatch(r'-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|true|false', value, re.I):
                extras[key] = value
    gpu = ''
    try:
        with (Path('/proc') / str(proc['pid']) / 'environ').open('rb') as stream:
            for value in stream.read(1048576).split(b'\0'):
                if value.startswith(b'CUDA_VISIBLE_DEVICES='):
                    gpu = value.split(b'=', 1)[1].decode('ascii', 'ignore')
        if re.fullmatch(r'\d+(?:\s*,\s*\d+)*', gpu):
            found['gpu_ids'] = gpu; sources['gpu_ids'] = 'CUDA_VISIBLE_DEVICES'
    except OSError:
        pass
    log = regular_log(resolve(found.get('log_path', ''), cwd))
    if not log:
        for member in members:
            for fd in ('1', '2'):
                try:
                    log = regular_log(Path(os.readlink(f'/proc/{member}/fd/{fd}')))
                except OSError:
                    pass
                if log:
                    sources['log_path'] = '训练进程 stdout/stderr'; break
            if log:
                break
    if not log and output_path:
        for name in ['train.log', 'training.log', 'log.txt', 'stdout.log', 'output.log']:
            log = regular_log(output_path / name)
            if log:
                sources['log_path'] = '输出目录日志（候选，需核对）'; break
    found.pop('log_path', None)
    if log:
        found['log_path'] = log
        # Saved effective startup config is useful while the output directory has
        # not yet been created (e.g. during a large model's initialization).
        values = startup_config(log)
        for field, aliases in ALIASES.items():
            value = next((values[k] for k in aliases if values.get(k)), '')
            if value and field not in found:
                found[field] = value
                sources[field] = '日志启动配置：' + log
        for key, value in values.items():
            if re.search(r'(?:lambda|weight|horizon|window|contrastive|dtw|hidden_dim|num_layers|grad_accum)', key) and not SECRET.search(key):
                if re.fullmatch(r'-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|true|false', value, re.I):
                    extras.setdefault(key, value)
    identity = hashlib.sha256((boot + ':' + str(proc['pid']) + ':' + proc['start']).encode()).hexdigest()
    script = next((Path(a).name for a in proc['argv'][1:] if a.endswith('.py')), '')
    if not script:
        for marker in ('--module', '-m'):
            if marker in proc['argv']:
                index = proc['argv'].index(marker) + 1
                if index < len(proc['argv']) and not proc['argv'][index].startswith('-'):
                    script = text(proc['argv'][index], 120)
                    break
    script = script or 'training'
    return {'pid': str(proc['pid']), 'worker_count': len(members), 'identity': identity,
            'script': script[:120], 'cwd': str(cwd)[:1024], 'fields': found,
            'sources': sources, 'extras': dict(list(extras.items())[:20])}


def main():
    selected = int(sys.argv[1]) if len(sys.argv) > 1 else None
    items = {}
    for path in list(Path('/proc').iterdir())[:8192]:
        if path.name.isdigit():
            proc = process(int(path.name))
            if proc:
                items[proc['pid']] = proc
    groups = {}
    for pid, proc in items.items():
        root, visited = pid, set()
        while items[root]['parent'] in items and root not in visited:
            visited.add(root); root = items[root]['parent']
        groups.setdefault(root, []).append(pid)
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:
        boot = 'unknown-boot'
    runs = []
    for pid, members in list(groups.items())[:20]:
        if selected and selected not in members:
            continue
        run = inspect_run(items[pid], members, boot)
        if len(json.dumps(runs + [run]).encode()) > 58000:
            break
        runs.append(run)
    print(json.dumps({'runs': runs}, ensure_ascii=True))


if __name__ == '__main__':
    main()
