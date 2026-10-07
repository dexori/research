"""Stage terminal/Markdown drafts locally. Never changes research records or SQLite."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import uuid

from git_sync import atomic_write, UUID, KINDS
from research_images import validate_images

MAX_BYTES = 2 * 1024 * 1024
SUFFIXES = {'.md', '.markdown', '.txt'}
EXPERIMENT_METADATA = {'model', 'dataset', 'seed', 'learning_rate', 'batch_size', 'total_steps', 'epochs',
                       'log_path', 'training_pid', 'gpu_ids', 'params', 'code_version',
                       'discovery_identity', 'discovery_server', 'discovery_sources', 'discovery_time'}


def decode_text(raw):
    if len(raw) > MAX_BYTES:
        raise ValueError('草稿文件超过 2 MB，请拆分后导入。')
    try:
        text = raw.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
    except UnicodeError:
        raise ValueError('草稿须为 UTF-8 编码的 Markdown 或文本文件。') from None
    if '\x00' in text or not text.strip():
        raise ValueError('草稿为空或含有无效文本字符。')
    return text


def inferred_title(content, source_name):
    heading = re.search(r'^#\s+(.+?)\s*#*\s*$', content, re.MULTILINE)
    return (heading.group(1).strip() if heading else Path(source_name).stem or '终端讨论草稿')[:240]


def validate(draft):
    if not isinstance(draft, dict) or draft.get('schema') != 1:
        raise ValueError('草稿格式无效。')
    if not isinstance(draft.get('id'), str) or not UUID.fullmatch(draft['id']):
        raise ValueError('草稿 ID 无效。')
    if draft.get('kind') not in KINDS:
        raise ValueError('请选择有效的记录类型。')
    for key, limit in [('title',240), ('tags',1000), ('source_name',240), ('created_at',50)]:
        if not isinstance(draft.get(key), str) or len(draft[key]) > limit:
            raise ValueError('草稿标题或元信息无效。')
    if not draft['title'].strip():
        raise ValueError('请填写草稿标题。')
    if draft.get('project_id') is not None and (
            not isinstance(draft['project_id'], str) or not UUID.fullmatch(draft['project_id'])):
        raise ValueError('项目 ID 须为平台中的完整记录 ID。')
    content = draft.get('content')
    if not isinstance(content, str):
        raise ValueError('草稿正文无效。')
    decode_text(content.encode('utf-8'))
    metadata=draft.get('metadata',{})
    validate_images(draft.get('images', []))
    allowed = {'knowledge_category','learning_topic','search_date','search_count','search_method'}
    if draft['kind'] == 'experiment':
        allowed |= EXPERIMENT_METADATA
    if (not isinstance(metadata,dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in metadata.items())
            or set(metadata)-allowed
            or len(json.dumps(metadata))>(16000 if draft['kind']=='experiment' else 4000)):
        raise ValueError('草稿元信息无效。')
    return draft


def stage_draft(data_dir, content, *, kind='idea', title='', project_id=None,
                tags='', source_name='终端讨论.md', metadata=None, images=None):
    content = decode_text(content.encode('utf-8'))
    # Only a display name is persisted, never an arbitrary server filesystem path.
    source_name = source_name.replace('\\','/').split('/')[-1][:240]
    draft = validate({'schema':1, 'id':uuid.uuid4().hex, 'kind':kind,
                      'title':title.strip() or inferred_title(content,source_name),
                      'project_id':None if kind=='project' else project_id,
                      'tags':tags, 'source_name':source_name,
                      'content':content, 'metadata':metadata or {}, 'images':images or [], 'created_at':datetime.now(timezone.utc).isoformat(timespec='microseconds')})
    cache = Path(data_dir)
    cache.mkdir(parents=True,exist_ok=True,mode=0o700)
    (cache/'import-drafts').mkdir(exist_ok=True,mode=0o700)
    atomic_write(cache/'import-drafts'/f"{draft['id']}.json",
                 json.dumps(draft,ensure_ascii=False).encode('utf-8'))
    return draft


def load_draft(data_dir, identifier):
    if not isinstance(identifier,str) or not UUID.fullmatch(identifier):
        raise ValueError('草稿 ID 无效。')
    path = Path(data_dir)/'import-drafts'/f'{identifier}.json'
    if path.is_symlink():
        raise ValueError('草稿不能是符号链接。')
    with path.open('rb') as stream:
        raw = stream.read(4*MAX_BYTES+1)
    if len(raw) > 4*MAX_BYTES:
        raise ValueError('草稿文件过大。')
    try:
        draft = validate(json.loads(raw.decode('utf-8')))
    except (UnicodeError, TypeError, KeyError, OverflowError, RecursionError):
        raise ValueError('草稿文件格式无效。') from None
    if draft['id'] != identifier:
        raise ValueError('草稿文件与 ID 不一致。')
    return draft


def list_drafts(data_dir, *, limit=100):
    folder = Path(data_dir)/'import-drafts'
    paths = sorted((path for path in folder.glob('*.json') if not path.is_symlink() and path.is_file()),
                   key=lambda path:path.stat().st_mtime_ns,reverse=True)
    drafts = []
    for path in paths[:500]:
        try:
            draft = load_draft(data_dir,path.stem)
        except (ValueError,OSError):
            continue
        drafts.append({key:value for key,value in draft.items() if key!='content'})
    return sorted(drafts,key=lambda draft:draft['created_at'],reverse=True)[:limit]
