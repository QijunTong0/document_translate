"""翻訳エンジンに渡すプロンプトの組み立てと，応答の取り出し．"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
DEFAULT_TRANSLATE_PROMPT = PROMPT_DIR / "translate_ja.md"
DEFAULT_GLOSSARY_PROMPT = PROMPT_DIR / "glossary_ja.md"

PUNCTUATION_STYLES = {
    "comma-period": "全角のコンマとピリオド（，．）",
    "touten-kuten": "読点と句点（、。）",
}

SOURCE_BEGIN, SOURCE_END = "<<<SOURCE_BEGIN>>>", "<<<SOURCE_END>>>"
OUT_BEGIN, OUT_END = "<<<TRANSLATION_BEGIN>>>", "<<<TRANSLATION_END>>>"
GLOSSARY_BEGIN, GLOSSARY_END = "<<<GLOSSARY_BEGIN>>>", "<<<GLOSSARY_END>>>"


class ResponseFormatError(Exception):
    pass


@dataclass
class PaperContext:
    title: str = ""
    abstract: str = ""
    headings: list[str] = field(default_factory=list)

    def render(self, max_abstract: int = 3000) -> str:
        parts = []
        if self.title:
            parts.append(f"タイトル: {self.title}")
        if self.abstract:
            parts.append("アブストラクト:\n" + self.abstract[:max_abstract])
        if self.headings:
            parts.append("章立て:\n" + "\n".join(self.headings))
        return "\n\n".join(parts)


@dataclass
class Glossary:
    entries: list[tuple[str, str, str]] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> "Glossary":
        entries = []
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            cols = [c.strip() for c in line.split("\t")]
            if len(cols) < 2 or not cols[0] or not cols[1]:
                continue
            entries.append((cols[0], cols[1], cols[2] if len(cols) > 2 else ""))
        return cls(entries)

    @classmethod
    def load(cls, path: Path) -> "Glossary":
        return cls.parse(path.read_text(encoding="utf-8"))

    def merged_with(self, override: "Glossary") -> "Glossary":
        """override の項目を優先して結合する．"""
        keys = {e[0].lower() for e in override.entries}
        return Glossary(list(override.entries) + [e for e in self.entries if e[0].lower() not in keys])

    def dump(self) -> str:
        header = "# 英語<TAB>訳語<TAB>備考（訳語が英語と同じ語は訳さない）\n"
        return header + "".join("\t".join(e) + "\n" for e in self.entries)

    def render(self) -> str:
        lines = []
        for en, ja, note in self.entries:
            target = "（英語のまま）" if en == ja else ja
            lines.append(f"- {en} → {target}" + (f"  ※{note}" if note else ""))
        return "\n".join(lines)


def load_guidelines(path: Path | None, punctuation: str, extra: list[str]) -> str:
    text = (path or DEFAULT_TRANSLATE_PROMPT).read_text(encoding="utf-8")
    text = text.replace("{{PUNCTUATION}}", PUNCTUATION_STYLES[punctuation])
    if extra:
        text = text.rstrip() + "\n\n## 追加の指示\n\n" + "\n\n".join(e.strip() for e in extra) + "\n"
    return text


def build_translation_prompt(
    guidelines: str,
    source: str,
    *,
    file_label: str,
    kind: str,
    position: str,
    paper_context: str = "",
    glossary: str = "",
    preceding: str = "",
    feedback: str = "",
) -> str:
    sections = [guidelines.rstrip()]
    if glossary:
        sections.append(
            "## 用語集\n\nこの論文では以下の訳語を用いる．「（英語のまま）」の語は訳さない．\n\n" + glossary
        )
    if paper_context:
        sections.append("## 論文の概要（文脈の把握用．翻訳対象ではない）\n\n" + paper_context)
    if preceding:
        sections.append(
            "## 直前の原文（文脈の把握用．翻訳対象ではない）\n\n"
            "<<<CONTEXT_BEGIN>>>\n" + preceding + "\n<<<CONTEXT_END>>>"
        )

    note = f"ファイル {file_label} の一部（論文全体の {position} 番目の断片）．"
    if kind == "preamble":
        note += (
            "\nこの断片は LaTeX のプリアンブルである．\\title，\\keywords，アブストラクト，"
            "\\thanks，\\newtheorem の表示名など，組版される文章だけを訳し，それ以外は一字一句変更しない．"
        )
    sections.append(f"## 翻訳対象\n\n{note}\n\n{SOURCE_BEGIN}\n{source}\n{SOURCE_END}")

    if feedback:
        sections.append(
            "## 前回の出力の問題点\n\n同じ断片に対する前回の出力には次の問題があった．"
            "これを修正したうえで，断片全体を改めて出力すること．\n\n" + feedback
        )

    sections.append(
        "## 出力形式\n\n"
        f"{SOURCE_BEGIN} と {SOURCE_END} の間の LaTeX ソースを翻訳し，訳文だけを次の形式で出力する．"
        "説明，前置き，コードブロック記法（```）は一切出力しない．"
        "翻訳すべき文章が含まれていない場合は，入力をそのまま出力する．\n\n"
        f"{OUT_BEGIN}\n（訳文）\n{OUT_END}"
    )
    return "\n\n".join(sections) + "\n"


def build_glossary_prompt(instructions: str, paper_text: str, paper_context: str,
                          user_glossary: str = "") -> str:
    sections = [instructions.rstrip()]
    if user_glossary:
        sections.append(
            "## 既定の訳語\n\n以下の語は訳語が決まっているので，用語集に含めなくてよい．"
            "他の語の訳語はこれと整合するように選ぶこと．\n\n" + user_glossary
        )
    if paper_context:
        sections.append("## 論文の概要\n\n" + paper_context)
    sections.append(f"## 論文のソース\n\n{SOURCE_BEGIN}\n{paper_text}\n{SOURCE_END}")
    sections.append(f"## 出力\n\n用語集を次の形式で出力する．\n\n{GLOSSARY_BEGIN}\n（用語集）\n{GLOSSARY_END}")
    return "\n\n".join(sections) + "\n"


def _between(text: str, begin: str, end: str) -> str:
    start = text.rfind(begin)
    if start < 0:
        raise ResponseFormatError(f"応答に {begin} が含まれていない")
    stop = text.find(end, start)
    if stop < 0:
        raise ResponseFormatError(f"応答に {end} が含まれていない（出力が途中で切れた可能性がある）")
    body = text[start + len(begin):stop]
    # まれにコードブロック記法で囲まれるので取り除く
    body = re.sub(r"^\s*```[A-Za-z]*\n", "", body)
    body = re.sub(r"\n```\s*$", "\n", body)
    return body.strip("\n")


def parse_translation(response: str) -> str:
    return _between(response, OUT_BEGIN, OUT_END)


def parse_glossary(response: str) -> Glossary:
    return Glossary.parse(_between(response, GLOSSARY_BEGIN, GLOSSARY_END))
