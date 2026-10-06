"""Local terminal draft importer. No model call, web token, or direct database write."""
import argparse
import json
import os
from pathlib import Path
import sys
import webbrowser

from draft_import import MAX_BYTES, SUFFIXES, decode_text, stage_draft
from git_sync import decode, KINDS, SyncError

ROOT = Path(__file__).resolve().parent


def main(argv=None):
    parser = argparse.ArgumentParser(description='科研工作台：终端草稿导入（保存前须在网页审核）')
    parser.add_argument('--data-dir',type=Path,default=Path(os.environ.get('RESEARCH_DATA_DIR',ROOT/'data')),
                        help='与平台相同的本机数据目录')
    parser.add_argument('--port',type=int,default=8765,help='本机平台的监听端口')
    sub = parser.add_subparsers(dest='command',required=True)
    importer = sub.add_parser('import',help='把一个 Markdown 文件暂存到网页草稿箱')
    importer.add_argument('file',type=Path)
    importer.add_argument('--kind',choices=sorted(KINDS),default='idea')
    importer.add_argument('--title',default='')
    importer.add_argument('--project',default=None,help='可选：完整项目 ID，运行 projects 查看')
    importer.add_argument('--tags',default='终端导入')
    importer.add_argument('--open',action='store_true',help='导入后打开本机网页预览')
    sub.add_parser('projects',help='从资料文件读取可选项目及其 ID')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('端口须在 1–65535 之间。')
    if args.command=='projects':
        for path in sorted((ROOT/'workspace/records').glob('*.md')):
            if path.is_symlink():
                continue
            try:
                with path.open('rb') as stream:
                    record = decode(stream.read(4*MAX_BYTES+1))
            except (SyncError,OSError):
                continue
            if record['kind']=='project' and record['status']!='已归档':
                print(record['id'],json.dumps(record['title'],ensure_ascii=False))
        return 0
    try:
        if args.file.suffix.lower() not in SUFFIXES:
            raise ValueError('请选择 .md、.markdown 或 .txt 草稿文件。')
        if not args.file.is_file():
            raise ValueError('草稿文件不存在或不是普通文件。')
        with args.file.open('rb') as stream:
            content = decode_text(stream.read(MAX_BYTES+1))
        draft = stage_draft(args.data_dir.resolve(),content,kind=args.kind,title=args.title,
                            project_id=args.project,tags=args.tags,source_name=args.file.name)
    except (ValueError,OSError) as error:
        # Filesystem exceptions can contain full local paths; don't publish them.
        print(str(error) if isinstance(error,ValueError) else '草稿无法读取或暂存，请检查路径与权限。',file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{args.port}/imports/{draft['id']}"
    print('草稿已暂存，尚未写入科研资料，也未上传 GitHub。')
    print('请在网页核对标题、类型、项目和正文，点击保存：')
    print(url)
    if args.open:
        webbrowser.open(url)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
