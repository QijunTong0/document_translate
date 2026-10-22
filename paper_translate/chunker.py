"""tex ファイルを翻訳単位（チャンク）に分割する．"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import texutil

# プリアンブルのうち，これらを含むチャンクだけを翻訳に回す
_PREAMBLE_TEXT_RE = re.compile(
    r"\\([A-Za-z]*title|abstract|keywords|newtheorem|thanks|date|subtitle)(?![A-Za-z@])"
)
_BLANK_LINES_RE = re.compile(r"(\n[ \t]*(?:\n[ \t]*)+\n?)")


@dataclass
class Chunk:
    file: str          # ソースのルートからの相対パス
    index: int         # ファイル内での通し番号
    kind: str          # "preamble" または "body"
    text: str          # 保護領域をプレースホルダに置き換えたテキスト
    translate: bool    # 翻訳エンジンに渡すかどうか

    @property
    def id(self) -> str:
        return f"{self.file}#{self.index}"


@dataclass
class FileUnit:
    rel: str
    regions: list[str]
    chunks: list[Chunk] = field(default_factory=list)

    def assemble(self, texts: list[str]) -> str:
        return texutil.restore_regions("".join(texts), self.regions)


def _blocks(text: str) -> list[str]:
    """空行を区切りとして段落単位のブロックに分ける（区切りは直前のブロックに含める）．"""
    parts = _BLANK_LINES_RE.split(text)
    blocks = []
    for i in range(0, len(parts), 2):
        block = parts[i] + (parts[i + 1] if i + 1 < len(parts) else "")
        if block:
            blocks.append(block)
    return blocks


def _pack(blocks: list[str], max_chars: int) -> list[str]:
    chunks, cur = [], ""
    for b in blocks:
        if cur and len(cur) + len(b) > max_chars:
            chunks.append(cur)
            cur = ""
        cur += b
    if cur:
        chunks.append(cur)
    return chunks


def _needs_translation(text: str, kind: str) -> bool:
    if kind == "preamble":
        return bool(_PREAMBLE_TEXT_RE.search(texutil.strip_comments(text)))
    return texutil.english_word_count(text) > 0


def split_file(rel: str, text: str, is_main: bool, max_chars: int,
               remove_comments: bool = True) -> FileUnit:
    if remove_comments:
        text = texutil.remove_comment_lines(text)
    masked, regions = texutil.protect_regions(text)
    unit = FileUnit(rel=rel, regions=regions)

    sections: list[tuple[str, str]] = []
    pos = texutil.begin_document_pos(masked) if is_main else -1
    if pos >= 0:
        sections.append(("preamble", masked[:pos]))
        sections.append(("body", masked[pos:]))
    else:
        sections.append(("body", masked))

    for kind, part in sections:
        for piece in _pack(_blocks(part), max_chars):
            unit.chunks.append(Chunk(
                file=rel, index=len(unit.chunks), kind=kind, text=piece,
                translate=_needs_translation(piece, kind),
            ))
    return unit


def merge_translation(original: str, translated: str) -> str:
    """訳文の前後の空白を原文に合わせる（チャンク連結時に段落区切りを崩さないため）．"""
    lead = re.match(r"\s*", original).group(0)
    trail = re.search(r"\s*$", original).group(0)
    return lead + translated.strip() + trail
