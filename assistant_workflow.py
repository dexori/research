"""Chat business logic, independent of Flask, SQLite and HTTP request state.

The caller selects the allowed context and owns task persistence. This workflow
returns an unsaved reply/draft; it never edits or synchronizes research records.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from codex_bridge import BridgeError
from draft_import import stage_draft
from research_images import illustration_prompt, insert_illustration, stage_image


@dataclass(frozen=True)
class ChatTurn:
    question: str
    title: str
    project_id: str | None
    history: list[dict]
    sources: list[dict]
    selection: dict[str, str]
    illustrated: bool = False


@dataclass(frozen=True)
class ChatReply:
    content: str
    error: str | None = None
    draft: dict | None = None


def generate_chat_reply(turn: ChatTurn, bridge, data_dir: Path,
                        on_progress: Callable[[dict], None] | None = None) -> ChatReply:
    def report(event):
        if on_progress:
            on_progress(event)

    try:
        options = {'on_progress': report} if on_progress else {}
        if turn.illustrated:
            options['illustrated'] = True
        content = bridge.answer(turn.question, turn.history, turn.sources,
                                **options, **turn.selection)
    except BridgeError as error:
        return ChatReply(str(error), error=str(error))
    except Exception:
        message = '本次回答未完成，问题已保留。请检查 Codex 登录与网络后重试。'
        return ChatReply(message, error=message)

    if not turn.illustrated:
        return ChatReply(content)

    report({'stage': 'illustrating', 'detail': '文字已返回，正在生成 img2 配图。', 'output': content})
    try:
        # Only the scoped public reply enters the image call, not all private notes.
        prompt = illustration_prompt(turn.title, content)
        raw = bridge.generate_image(prompt, on_progress=report, **turn.selection)
        image = stage_image(data_dir, raw, prompt)
        illustrated = insert_illustration(content, image)
        draft = stage_draft(data_dir, illustrated, kind='idea', title=turn.title,
                            project_id=turn.project_id, tags='AI 草稿',
                            source_name='科研聊天配图（AI 草稿）', images=[image])
    except Exception:
        # A failed image/save must not leave a broken URL in an otherwise useful reply.
        content += '\n\n> 配图未完成，文字已保留。请检查 Codex 生图额度或网络后重试。\n'
        report({'stage': 'layout', 'detail': '配图未完成，已保留文字回复。', 'output': content})
        return ChatReply(content)

    report({'stage': 'layout', 'detail': '图片已生成，正在排版图文回复。', 'output': illustrated})
    return ChatReply(illustrated, draft=draft)
