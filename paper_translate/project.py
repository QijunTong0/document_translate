"""論文ソースの展開と，翻訳対象となる tex ファイルの特定．"""

from __future__ import annotations

import fnmatch
import gzip
import os
import re
import shutil
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from . import texutil

JAPANESE_CLASS_PREFIXES = ("ltj", "jlreq", "bxjs")


class ProjectError(Exception):
    pass


# ---------------------------------------------------------------------------
# 展開


def _safe_target(root: Path, name: str) -> Path:
    target = (root / name).resolve()
    if not str(target).startswith(str(root.resolve()) + os.sep) and target != root.resolve():
        raise ProjectError(f"アーカイブに不正なパスが含まれています: {name}")
    return target


def extract_source(src: Path, dest: Path) -> Path:
    """zip / tar(.gz) / 単一の .gz / ディレクトリを dest に展開し，ソースのルートを返す．"""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True)
    elif zipfile.is_zipfile(src):
        with zipfile.ZipFile(src) as zf:
            for info in zf.infolist():
                target = _safe_target(dest, info.filename)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as fin, open(target, "wb") as fout:
                    shutil.copyfileobj(fin, fout)
    elif tarfile.is_tarfile(src):
        with tarfile.open(src) as tf:
            for member in tf.getmembers():
                _safe_target(dest, member.name)
                if not (member.isfile() or member.isdir()):
                    continue
                if hasattr(tarfile, "data_filter"):
                    tf.extract(member, dest, filter="data")
                else:
                    tf.extract(member, dest)
    elif src.suffix == ".gz":
        # arXiv では tex ファイル 1 つだけのソースが gzip 単体で配布されることがある
        name = src.stem if src.stem.endswith(".tex") else "main.tex"
        with gzip.open(src, "rb") as fin, open(dest / name, "wb") as fout:
            shutil.copyfileobj(fin, fout)
    elif src.suffix == ".tex":
        shutil.copy2(src, dest / src.name)
    else:
        raise ProjectError(f"対応していない入力形式です: {src}")

    # 中身が 1 つのディレクトリに包まれている場合はその中をルートとする
    root = dest
    while True:
        entries = [p for p in root.iterdir() if p.name not in ("__MACOSX", ".DS_Store")]
        if len(entries) == 1 and entries[0].is_dir():
            root = entries[0]
        else:
            break
    return root


# ---------------------------------------------------------------------------
# 構成の解析


def read_tex(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ProjectError(f"文字コードを判別できません: {path}")


def find_main_tex(root: Path) -> Path:
    candidates = []
    for path in sorted(root.rglob("*.tex")):
        text = texutil.strip_comments(read_tex(path))
        if re.search(r"\\documentclass", text) and re.search(r"\\begin\s*\{document\}", text):
            candidates.append(path)
    if not candidates:
        raise ProjectError("\\documentclass と \\begin{document} を含む tex ファイルが見つかりません．--main で指定してください．")
    if len(candidates) == 1:
        return candidates[0]
    for name in ("main.tex", "paper.tex", "ms.tex", "manuscript.tex"):
        for c in candidates:
            if c.name == name:
                return c
    # ルートに近く，最も大きいファイルを選ぶ
    return sorted(candidates, key=lambda p: (len(p.relative_to(root).parts), -p.stat().st_size))[0]


_INCLUDE_RE = re.compile(
    r"\\(input|include|subfile)\s*\{([^}]+)\}"
    r"|\\input\s+([^\s{}\\%]+)"
    r"|\\(import|subimport|inputfrom|includefrom|subinputfrom|subincludefrom)\*?\s*\{([^}]*)\}\s*\{([^}]+)\}"
)


def _resolve(base: Path, name: str) -> Path | None:
    name = name.strip()
    for cand in (base / name, base / (name + ".tex")):
        if cand.is_file() and cand.suffix == ".tex":
            return cand
    return None


def find_includes(path: Path, root: Path) -> list[Path]:
    """path から \\input などで読み込まれる tex ファイルを出現順に返す．"""
    text = texutil.strip_comments(read_tex(path))
    found = []
    for m in _INCLUDE_RE.finditer(text):
        if m.group(2):
            # LaTeX は相対パスをコンパイル時の作業ディレクトリ（主ファイルの場所）から解決する
            target = _resolve(root, m.group(2)) or _resolve(path.parent, m.group(2))
        elif m.group(3):
            target = _resolve(root, m.group(3)) or _resolve(path.parent, m.group(3))
        else:
            cmd, directory, name = m.group(4), m.group(5), m.group(6)
            base = path.parent if cmd.startswith("sub") else root
            target = _resolve(base / directory, name) or _resolve(root / directory, name)
        if target:
            found.append(target)
    return found


def collect_tex_files(main: Path) -> list[Path]:
    """主ファイルから辿れる tex ファイルを文書中の出現順で返す．"""
    root = main.parent
    order: list[Path] = []
    seen: set[Path] = set()

    def visit(p: Path) -> None:
        rp = p.resolve()
        if rp in seen:
            return
        seen.add(rp)
        order.append(p)
        for child in find_includes(p, root):
            visit(child)

    visit(main)
    return order


_DEFINITION_LINE_RE = re.compile(
    r"^\s*\\(newcommand|renewcommand|providecommand|def|gdef|edef|let|DeclareMathOperator|"
    r"newtheorem|theoremstyle|usepackage|RequirePackage|definecolor|newenvironment|"
    r"renewenvironment|setlength|addtolength|tikzset|pgfplotsset|makeatletter|makeatother|"
    r"newif|newcounter|setcounter|crefname|Crefname|DeclareRobustCommand|NewDocumentCommand)"
)


def has_prose(text: str) -> bool:
    """訳すべき文章を含んでいそうかを判定する．

    マクロ定義だけのファイルや，TikZ などで描かれた図だけのファイルを
    翻訳対象から外すために用いる．
    """
    stripped = texutil.strip_comments(texutil.protect_regions(text)[0])
    if re.search(r"\\(sub)*section|\\caption|\\paragraph|\\chapter|\\begin\{abstract\}", stripped):
        return True
    lines = [l for l in stripped.split("\n") if l.strip()]
    if not lines:
        return False
    def_ratio = sum(1 for l in lines if _DEFINITION_LINE_RE.match(l)) / len(lines)
    return def_ratio < 0.5 and texutil.english_word_count(stripped) >= 30


@dataclass
class TexProject:
    root: Path
    main: Path
    files: list[Path]            # 翻訳対象（出現順）
    skipped: list[Path] = field(default_factory=list)

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()


def analyze(root: Path, main: str | None = None, include: list[str] | None = None,
            exclude: list[str] | None = None) -> TexProject:
    if main:
        main_path = root / main
        if not main_path.is_file():
            raise ProjectError(f"主ファイルが見つかりません: {main}")
    else:
        main_path = find_main_tex(root)

    files = collect_tex_files(main_path)
    rel = lambda p: p.relative_to(root).as_posix()
    if include:
        extra = [p for p in sorted(root.rglob("*.tex"))
                 if any(fnmatch.fnmatch(rel(p), g) for g in include) and p not in files]
        files += extra

    selected, skipped = [], []
    for p in files:
        if exclude and any(fnmatch.fnmatch(rel(p), g) for g in exclude):
            skipped.append(p)
        elif p == main_path or (include and any(fnmatch.fnmatch(rel(p), g) for g in include)):
            selected.append(p)
        elif has_prose(read_tex(p)):
            selected.append(p)
        else:
            skipped.append(p)
    return TexProject(root=root, main=main_path, files=selected, skipped=skipped)


# ---------------------------------------------------------------------------
# 文脈情報の抽出


def extract_title(text: str) -> str:
    t = texutil.strip_comments(text)
    m = texutil.find_command(t, "title")
    if not m:
        return ""
    pos = m.end()
    opt = texutil.read_group(t, pos, "[", "]")
    if opt:
        pos = opt[1]
    g = texutil.read_group(t, pos)
    return " ".join(g[0].split()) if g else ""


def extract_abstract(text: str) -> str:
    t = texutil.strip_comments(text)
    m = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", t, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = texutil.find_command(t, "abstract")
    if m:
        g = texutil.read_group(t, m.end())
        if g:
            return g[0].strip()
    return ""


def extract_headings(text: str) -> list[str]:
    t = texutil.strip_comments(text)
    out = []
    for m in re.finditer(r"\\((?:sub){0,2}section|chapter)\*?\s*(?:\[[^\]]*\])?", t):
        g = texutil.read_group(t, m.end())
        if g:
            indent = "  " * m.group(1).count("sub")
            out.append(indent + " ".join(g[0].split()))
    return out


def is_japanese_class(class_name: str) -> bool:
    return class_name.startswith(JAPANESE_CLASS_PREFIXES)
