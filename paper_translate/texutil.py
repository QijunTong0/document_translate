"""LaTeX ソースを扱うための小さな字句処理ユーティリティ．"""

from __future__ import annotations

import re
from collections import Counter

# 中身を翻訳せず，そのまま保護する環境
PROTECTED_ENVS = (
    "verbatim", "verbatim*", "Verbatim", "BVerbatim", "LVerbatim",
    "lstlisting", "minted", "alltt", "comment",
    "filecontents", "filecontents*",
    "thebibliography",
    "tikzpicture", "pgfpicture", "forest", "tikzcd",
)

# 中身をそのまま書き出す（% がコメントにならない）環境
VERBATIM_LIKE_ENVS = (
    "verbatim", "verbatim*", "Verbatim", "BVerbatim", "LVerbatim",
    "lstlisting", "minted", "alltt", "comment",
    "filecontents", "filecontents*",
)

PLACEHOLDER_FMT = "[[[KEEP_{:04d}]]]"
PLACEHOLDER_RE = re.compile(r"\[\[\[KEEP_(\d{4})\]\]\]")

_ESCAPED = r"(?<!\\)(?:\\\\)*"


def comment_start(line: str) -> int:
    """行内でコメントを始める % の位置を返す．無ければ -1．"""
    i = 0
    while i < len(line):
        c = line[i]
        if c == "\\":
            i += 2
            continue
        if c == "%":
            return i
        i += 1
    return -1


def strip_comments(text: str) -> str:
    """すべてのコメントを除去する（解析用．verbatim 環境は考慮しない）．"""
    out = []
    for line in text.split("\n"):
        pos = comment_start(line)
        out.append(line if pos < 0 else line[:pos])
    return "\n".join(out)


def _verbatim_line_flags(lines: list[str]) -> list[bool]:
    """各行が verbatim 系環境の内部（開始・終了行を含む）にあるかを返す．"""
    begin_re = re.compile(
        r"\\begin\{(" + "|".join(re.escape(e) for e in VERBATIM_LIKE_ENVS) + r")\}"
    )
    flags = []
    current = None
    for line in lines:
        if current is None:
            pos = comment_start(line)
            m = begin_re.search(line if pos < 0 else line[:pos])
            if m:
                current = m.group(1)
                flags.append(True)
                if ("\\end{%s}" % current) in line[m.end():]:
                    current = None
                continue
            flags.append(False)
        else:
            flags.append(True)
            if ("\\end{%s}" % current) in line:
                current = None
    return flags


def remove_comment_lines(text: str) -> str:
    """コメントだけからなる行を削除する．

    行頭の空白に続く % は行末の改行まで読み飛ばされるため，この行を丸ごと
    削除しても組版結果は変わらない．verbatim 系環境の内部は対象外とする．
    """
    lines = text.split("\n")
    flags = _verbatim_line_flags(lines)
    kept = [
        line for line, in_verb in zip(lines, flags)
        if in_verb or not re.match(r"^[ \t]*%", line)
    ]
    return "\n".join(kept)


def read_group(text: str, pos: int, open_ch: str = "{", close_ch: str = "}") -> tuple[str, int] | None:
    """text[pos] から始まる（空白を飛ばした）括弧グループの中身と終了位置を返す．"""
    i = pos
    while i < len(text) and text[i] in " \t\n":
        i += 1
    if i >= len(text) or text[i] != open_ch:
        return None
    depth = 0
    j = i
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return text[i + 1 : j], j + 1
        j += 1
    return None


def find_command(text: str, name: str, start: int = 0) -> re.Match | None:
    """コメント外にある \\name の最初の出現を探す．text はコメント除去済みを想定．"""
    return re.compile(r"\\" + re.escape(name) + r"(?![A-Za-z@])").search(text, start)


def documentclass_span(text: str) -> tuple[int, int, str] | None:
    """\\documentclass[...]{...} の (開始位置, 終了位置, クラス名) を返す．

    位置は元の text 上の位置．コメント中の \\documentclass は無視する．
    """
    masked = _mask_comments_keep_length(text)
    m = find_command(masked, "documentclass")
    if not m:
        return None
    pos = m.end()
    opt = read_group(masked, pos, "[", "]")
    if opt:
        pos = opt[1]
    arg = read_group(masked, pos)
    if not arg:
        return None
    return m.start(), arg[1], arg[0].strip()


def begin_document_pos(text: str) -> int:
    """コメント外にある \\begin{document} の位置．無ければ -1．"""
    masked = _mask_comments_keep_length(text)
    m = re.search(r"\\begin\s*\{document\}", masked)
    return m.start() if m else -1


def _mask_comments_keep_length(text: str) -> str:
    """コメント部分を空白で置き換える（位置を保ったまま解析するため）．"""
    out = []
    for line in text.split("\n"):
        pos = comment_start(line)
        out.append(line if pos < 0 else line[:pos] + " " * (len(line) - pos))
    return "\n".join(out)


def protect_regions(text: str) -> tuple[str, list[str]]:
    """翻訳させない領域をプレースホルダに置き換える．

    返り値は (置換後のテキスト, 元の領域のリスト)．i 番目の領域は
    PLACEHOLDER_FMT.format(i) に置き換わる．
    """
    regions: list[str] = []

    def repl(m: re.Match) -> str:
        regions.append(m.group(0))
        return PLACEHOLDER_FMT.format(len(regions) - 1)

    env_alt = "|".join(re.escape(e) for e in PROTECTED_ENVS)
    env_re = re.compile(
        r"\\begin\{(" + env_alt + r")\}.*?\\end\{\1\}", re.DOTALL
    )
    text = env_re.sub(repl, text)
    # \verb|...| などのインラインのコード
    text = re.sub(r"\\verb\*?([^A-Za-z\s])[^\n]*?\1", repl, text)
    return text, regions


def restore_regions(text: str, regions: list[str]) -> str:
    return PLACEHOLDER_RE.sub(lambda m: regions[int(m.group(1))], text)


# ---------------------------------------------------------------------------
# 構造の比較（訳文の検証用）


def _without_protected(text: str) -> str:
    return PLACEHOLDER_RE.sub("", text)


def env_counter(text: str) -> Counter:
    t = strip_comments(_without_protected(text))
    c: Counter = Counter()
    for kind, name in re.findall(r"\\(begin|end)\s*\{([^}]*)\}", t):
        c[(kind, name.strip())] += 1
    return c


def brace_balance(text: str) -> int:
    """エスケープされていない { と } の数の差．"""
    t = strip_comments(_without_protected(text))
    t = re.sub(r"\\.", "", t)
    return t.count("{") - t.count("}")


def dollar_count(text: str) -> int:
    t = strip_comments(_without_protected(text))
    t = re.sub(r"\\.", "", t)
    return t.count("$")


_KEY_CMD_RE = re.compile(
    r"\\([A-Za-z]*(?:cite|ref)[A-Za-z]*|label)\*?\s*(?:\[[^\]]*\]\s*){0,2}\{([^}]*)\}"
)


def key_counter(text: str, kind: str) -> Counter:
    """\\label / \\ref 系 / \\cite 系のキーを数える．kind は 'label', 'ref', 'cite'．"""
    t = strip_comments(_without_protected(text))
    c: Counter = Counter()
    for cmd, keys in _KEY_CMD_RE.findall(t):
        if kind == "label" and cmd != "label":
            continue
        if kind == "cite" and "cite" not in cmd:
            continue
        if kind == "ref" and (cmd == "label" or "cite" in cmd):
            continue
        for k in keys.split(","):
            k = k.strip()
            if k:
                c[k] += 1
    return c


def english_word_count(text: str) -> int:
    """数式・コマンド・保護領域を除いた英単語（3文字以上）の数．"""
    t = strip_comments(_without_protected(text))
    t = re.sub(r"\$\$.*?\$\$|\$[^$]*\$|\\\[.*?\\\]|\\\(.*?\\\)", " ", t, flags=re.DOTALL)
    t = re.sub(r"\\(label|ref|eqref|cref|Cref|autoref|cite[a-z]*|url|href|input|include|"
               r"includegraphics|begin|end|usepackage|documentclass)\*?(\[[^\]]*\])?\{[^}]*\}", " ", t)
    t = re.sub(r"\\[A-Za-z@]+\*?", " ", t)
    return len(re.findall(r"[A-Za-z]{3,}", t))


def japanese_char_count(text: str) -> int:
    return len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff]", _without_protected(text)))
