"""日本語組版の設定の挿入と，Docker 上での LuaLaTeX によるコンパイル．"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import project, texutil

DOCKER_DIR = Path(__file__).resolve().parent.parent / "docker"
DEFAULT_IMAGE = "paper-translate-texlive-ja"

# LuaTeX には pdfTeX の \pdfoutput が無いため，arXiv のソースによくある
# \pdfoutput=1 がエラーにならないよう \outputmode の別名にしておく
ENGINE_COMPAT = (
    "\\ifdefined\\pdfoutput\\else\\ifdefined\\outputmode\\let\\pdfoutput\\outputmode\\fi\\fi\n"
)

JAPANESE_PACKAGES = "\\usepackage{luatexja}"

LOCALIZED_NAMES = r"""\makeatletter
\AtBeginDocument{%
  \ifdefined\figurename\renewcommand{\figurename}{図}\fi
  \ifdefined\tablename\renewcommand{\tablename}{表}\fi
  \ifdefined\abstractname\renewcommand{\abstractname}{概要}\fi
  \ifdefined\refname\renewcommand{\refname}{参考文献}\fi
  \ifdefined\bibname\renewcommand{\bibname}{参考文献}\fi
  \ifdefined\contentsname\renewcommand{\contentsname}{目次}\fi
  \ifdefined\appendixname\renewcommand{\appendixname}{付録}\fi
  \ifdefined\proofname\renewcommand{\proofname}{証明}\fi
  \ifdefined\algorithmname\renewcommand{\algorithmname}{アルゴリズム}\fi
  \@ifpackageloaded{hyperref}{%
    \def\figureautorefname{図}\def\tableautorefname{表}%
    \def\sectionautorefname{節}\def\subsectionautorefname{節}%
    \def\subsubsectionautorefname{節}\def\equationautorefname{式}%
    \def\appendixautorefname{付録}\def\algorithmautorefname{アルゴリズム}%
  }{}%
  \@ifpackageloaded{cleveref}{%
    \crefname{figure}{図}{図}\Crefname{figure}{図}{図}%
    \crefname{table}{表}{表}\Crefname{table}{表}{表}%
    \crefname{section}{節}{節}\Crefname{section}{節}{節}%
    \crefname{subsection}{節}{節}\Crefname{subsection}{節}{節}%
    \crefname{subsubsection}{節}{節}\Crefname{subsubsection}{節}{節}%
    \crefname{appendix}{付録}{付録}\Crefname{appendix}{付録}{付録}%
    \crefname{equation}{式}{式}\Crefname{equation}{式}{式}%
    \crefname{algorithm}{アルゴリズム}{アルゴリズム}\Crefname{algorithm}{アルゴリズム}{アルゴリズム}%
  }{}%
}
\makeatother
"""


def inject_japanese_support(text: str, localize_names: bool = True) -> str:
    """主ファイルに LuaTeX-ja の読み込みと，図表名などの日本語化を挿入する．"""
    span = texutil.documentclass_span(text)
    if span is None:
        raise project.ProjectError("主ファイルに \\documentclass が見つかりません")
    _, dc_end, class_name = span
    already = re.search(r"\\usepackage(\[[^\]]*\])?\{[^}]*luatexja", texutil.strip_comments(text))
    if not (project.is_japanese_class(class_name) or already):
        text = text[:dc_end] + "\n" + JAPANESE_PACKAGES + text[dc_end:]
    if localize_names:
        pos = texutil.begin_document_pos(text)
        if pos >= 0:
            text = text[:pos] + LOCALIZED_NAMES + text[pos:]
    return ENGINE_COMPAT + text


# ---------------------------------------------------------------------------
# コンパイル


class BuildError(Exception):
    pass


@dataclass
class BuildResult:
    pdf: Path | None
    log_path: Path | None
    errors: list[str]
    returncode: int


def latexmk_command(main_name: str, shell_escape: bool) -> list[str]:
    cmd = ["latexmk", "-lualatex", "-f", "-interaction=nonstopmode", "-file-line-error"]
    if shell_escape:
        cmd.append("-shell-escape")
    return cmd + [main_name]


def ensure_image(image: str, rebuild: bool = False) -> None:
    if not shutil.which("docker"):
        raise BuildError("docker コマンドが見つかりません．Docker をインストールして起動してください．")
    if not rebuild:
        found = subprocess.run(["docker", "image", "inspect", image],
                               capture_output=True, text=True)
        if found.returncode == 0:
            return
    print(f"Docker イメージ {image} を作成します（初回は TeX Live の取得に時間がかかります）...", flush=True)
    proc = subprocess.run(["docker", "build", "-t", image, str(DOCKER_DIR)])
    if proc.returncode != 0:
        raise BuildError("Docker イメージの作成に失敗しました")


def build_pdf(src_root: Path, main_rel: str, *, use_docker: bool = True,
              image: str = DEFAULT_IMAGE, rebuild_image: bool = False,
              shell_escape: bool = False, timeout: float = 1800) -> BuildResult:
    main_path = src_root / main_rel
    workdir = main_path.parent
    cmd = latexmk_command(main_path.name, shell_escape)

    if use_docker:
        ensure_image(image, rebuild_image)
        docker = ["docker", "run", "--rm", "--network", "none",
                  "-v", f"{src_root.resolve()}:/work",
                  "-w", "/work/" + Path(main_rel).parent.as_posix()]
        if hasattr(os, "getuid"):
            docker += ["-u", f"{os.getuid()}:{os.getgid()}"]
        cmd = docker + [image] + cmd
        cwd = None
    else:
        if not shutil.which("latexmk"):
            raise BuildError("latexmk が見つかりません")
        cwd = workdir

    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise BuildError(f"コンパイルが {timeout:.0f} 秒以内に終了しませんでした") from e

    (workdir / "latexmk-output.txt").write_text(proc.stdout + proc.stderr, encoding="utf-8")
    pdf = main_path.with_suffix(".pdf")
    log_path = main_path.with_suffix(".log")
    errors: list[str] = []
    if log_path.exists():
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        errors = [l for l in log_text.splitlines()
                  if l.startswith("! ") or re.match(r"^\S+:\d+: ", l)]
    return BuildResult(pdf if pdf.exists() else None, log_path if log_path.exists() else None,
                       errors, proc.returncode)
