"""コマンドラインのエントリポイント．"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path

from . import chunker, latex, project, prompts
from .engines import EngineError, create_engine
from .translator import TranslationAborted, Translator, generate_glossary, log

ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".zip", ".gz")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="translate_paper.py",
        description="英語論文の LaTeX ソースを日本語に翻訳し，Docker 上の LuaLaTeX で PDF を作成する．",
    )
    p.add_argument("input", type=Path, help="論文の LaTeX ソース（zip，tar.gz，ディレクトリ，tex ファイル）")
    p.add_argument("-o", "--output-dir", type=Path, default=Path("."),
                   help="PDF と翻訳済みソースの出力先（既定: カレントディレクトリ）")
    p.add_argument("--workdir", type=Path,
                   help="作業ディレクトリ．キャッシュと用語集が置かれ，再実行時に再利用される"
                        "（既定: <出力先>/<入力名>_ja_work）")

    g = p.add_argument_group("翻訳エンジン")
    g.add_argument("--engine", choices=["claude", "codex", "command"], default="claude",
                   help="claude: Claude Code（claude -p），codex: Codex（codex exec），"
                        "command: --engine-cmd で指定したコマンド（既定: claude）")
    g.add_argument("--model", help="エンジンに渡すモデル名")
    g.add_argument("--engine-arg", action="append", default=[], metavar="ARG",
                   help="エンジンのコマンドに追加する引数（複数指定可）")
    g.add_argument("--engine-cmd", help="--engine command のときのコマンド．プロンプトを標準入力で受け取り，応答を標準出力に書くもの")
    g.add_argument("-j", "--jobs", type=int, default=3, help="同時に実行する翻訳の数（既定: 3）")
    g.add_argument("--timeout", type=float, default=1200, help="1 回の呼び出しの制限時間（秒）")
    g.add_argument("--retries", type=int, default=2, help="訳文の検査に失敗したときの再試行回数")
    g.add_argument("--no-cache", action="store_true", help="過去の翻訳結果を使わずにすべて翻訳し直す")

    g = p.add_argument_group("プロンプト")
    g.add_argument("--prompt", type=Path,
                   help="翻訳の指示（既定: prompts/translate_ja.md）を置き換えるファイル")
    g.add_argument("--prompt-append", type=Path, action="append", default=[], metavar="FILE",
                   help="翻訳の指示に追記する内容を書いたファイル（複数指定可）")
    g.add_argument("--instruction", action="append", default=[], metavar="TEXT",
                   help="翻訳の指示に追記する短い指示（複数指定可）")
    g.add_argument("--punctuation", choices=sorted(prompts.PUNCTUATION_STYLES), default="comma-period",
                   help="句読点の種類．comma-period は「，．」，touten-kuten は「、。」（既定: comma-period）")
    g.add_argument("--glossary", type=Path,
                   help="訳語を指定する用語集（1 行に「英語<TAB>訳語<TAB>備考」）．自動生成の用語集より優先する")
    g.add_argument("--no-auto-glossary", action="store_true", help="用語集を自動生成しない")
    g.add_argument("--regenerate-glossary", action="store_true",
                   help="作業ディレクトリにある自動生成済みの用語集を作り直す")
    g.add_argument("--glossary-prompt", type=Path,
                   help="用語集生成の指示（既定: prompts/glossary_ja.md）を置き換えるファイル")
    g.add_argument("--glossary-source-chars", type=int, default=100_000,
                   help="用語集の生成時に渡す本文の最大文字数")

    g = p.add_argument_group("ソースの扱い")
    g.add_argument("--main", help="主ファイルのパス（ソースのルートからの相対パス）．省略時は自動判定")
    g.add_argument("--include", action="append", default=[], metavar="GLOB",
                   help="必ず翻訳する tex ファイル（ソースのルートからの相対パスのパターン）")
    g.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                   help="翻訳しない tex ファイル（同上）")
    g.add_argument("--chunk-chars", type=int, default=8000, help="1 回の翻訳で渡す最大文字数の目安")
    g.add_argument("--keep-comments", action="store_true",
                   help="コメント行を削除せずに残す（既定では翻訳前に削除する）")
    g.add_argument("--keep-english-names", action="store_true",
                   help="Figure，Table などの見出し語を日本語化しない")

    g = p.add_argument_group("コンパイル")
    g.add_argument("--no-build", action="store_true", help="翻訳だけ行い，PDF を作らない")
    g.add_argument("--build-only", action="store_true",
                   help="翻訳をせず，作業ディレクトリにある翻訳済みソースをコンパイルし直す")
    g.add_argument("--no-docker", action="store_true", help="Docker を使わず，手元の latexmk でコンパイルする")
    g.add_argument("--docker-image", default=latex.DEFAULT_IMAGE, help="コンパイルに使う Docker イメージ名")
    g.add_argument("--rebuild-image", action="store_true", help="Docker イメージを作り直す")
    g.add_argument("--shell-escape", action="store_true", help="-shell-escape を付けてコンパイルする（minted など）")

    p.add_argument("--dry-run", action="store_true",
                   help="翻訳対象のファイルと分割結果を表示するだけで終了する")
    return p.parse_args(argv)


def input_stem(path: Path) -> str:
    name = path.name
    for suf in ARCHIVE_SUFFIXES:
        if name.lower().endswith(suf):
            return name[: -len(suf)]
    return path.stem if path.is_file() else name


def zip_dir(src: Path, dest: Path) -> None:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(src).as_posix())


def build(args: argparse.Namespace, ja_root: Path, main_rel: str, stem: str) -> int:
    log("PDF をコンパイルしています...")
    result = latex.build_pdf(
        ja_root, main_rel, use_docker=not args.no_docker, image=args.docker_image,
        rebuild_image=args.rebuild_image, shell_escape=args.shell_escape,
    )
    if result.errors:
        log(f"LaTeX のエラーが {len(result.errors)} 件あります（ログ: {result.log_path}）:")
        for e in result.errors[:10]:
            log("  " + e)
    if result.pdf is None:
        log(f"PDF を作成できませんでした．{ja_root / 'latexmk-output.txt'} を確認してください．")
        return 1
    out_pdf = args.output_dir / f"{stem}_ja.pdf"
    shutil.copy2(result.pdf, out_pdf)
    log(f"PDF: {out_pdf}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except (project.ProjectError, EngineError, latex.BuildError, TranslationAborted) as e:
        log(f"エラー: {e}")
        return 1
    except KeyboardInterrupt:
        log("中断しました．再実行すると翻訳済みの部分はキャッシュから再利用されます．")
        return 130


def run(args: argparse.Namespace) -> int:
    if not args.input.exists():
        raise project.ProjectError(f"入力が見つかりません: {args.input}")
    stem = input_stem(args.input)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    workdir = args.workdir or args.output_dir / f"{stem}_ja_work"
    workdir.mkdir(parents=True, exist_ok=True)
    ja_dir = workdir / "ja"
    state_path = workdir / "state.json"

    if args.build_only:
        if not state_path.exists():
            raise project.ProjectError("翻訳済みのソースがありません．先に翻訳を実行してください．")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        return build(args, ja_dir / state["root"], state["main"], stem)

    # 1. 展開と解析
    src_root = project.extract_source(args.input, workdir / "src")
    proj = project.analyze(src_root, args.main, args.include, args.exclude)
    log(f"主ファイル: {proj.rel(proj.main)}")
    log("翻訳するファイル: " + ", ".join(proj.rel(f) for f in proj.files))
    if proj.skipped:
        log("翻訳しないファイル: " + ", ".join(proj.rel(f) for f in proj.skipped))

    units = [
        chunker.split_file(proj.rel(f), project.read_tex(f), f == proj.main,
                           args.chunk_chars, remove_comments=not args.keep_comments)
        for f in proj.files
    ]
    n_targets = sum(c.translate for u in units for c in u.chunks)
    log(f"翻訳する断片: {n_targets} 個")

    if args.dry_run:
        for u in units:
            for c in u.chunks:
                mark = "翻訳" if c.translate else "そのまま"
                print(f"{c.id}\t{c.kind}\t{len(c.text)} 文字\t{mark}")
        return 0

    # 2. エンジンとプロンプトの準備
    engine = create_engine(args.engine, model=args.model, extra_args=args.engine_arg,
                           command=args.engine_cmd, timeout=args.timeout)
    engine.check()
    extra = [p.read_text(encoding="utf-8") for p in args.prompt_append] + args.instruction
    guidelines = prompts.load_guidelines(args.prompt, args.punctuation, extra)

    all_text = "\n".join(project.read_tex(f) for f in proj.files)
    context = prompts.PaperContext(
        title=project.extract_title(project.read_tex(proj.main)),
        abstract=project.extract_abstract(all_text),
        headings=project.extract_headings(all_text),
    ).render()

    # 3. 用語集
    user_glossary = prompts.Glossary.load(args.glossary) if args.glossary else prompts.Glossary()
    auto_path = workdir / "glossary.tsv"
    auto_glossary = prompts.Glossary()
    if not args.no_auto_glossary:
        if auto_path.exists() and not args.regenerate_glossary:
            log(f"作成済みの用語集を使います: {auto_path}")
            auto_glossary = prompts.Glossary.load(auto_path)
        else:
            log("用語集を作成しています...")
            instructions = (args.glossary_prompt or prompts.DEFAULT_GLOSSARY_PROMPT).read_text(encoding="utf-8")
            auto_glossary = generate_glossary(engine, instructions, units, context,
                                              user_glossary.render(), args.glossary_source_chars,
                                              workdir / "logs")
            auto_path.write_text(auto_glossary.dump(), encoding="utf-8")
            log(f"用語集を保存しました（{len(auto_glossary.entries)} 語）: {auto_path}")
            log("  用語集を編集して再実行すると，その訳語で翻訳し直します．")
    glossary = auto_glossary.merged_with(user_glossary)

    # 4. 翻訳
    translator = Translator(engine, guidelines, workdir, glossary=glossary.render(),
                            paper_context=context, jobs=args.jobs, retries=args.retries,
                            use_cache=not args.no_cache)
    results = translator.translate_all(units)

    # 5. 翻訳済みソースの書き出し
    if ja_dir.exists():
        shutil.rmtree(ja_dir)
    shutil.copytree(src_root, ja_dir)
    ja_root = ja_dir
    root_rel = src_root.relative_to(workdir / "src").as_posix()
    if root_rel != ".":
        ja_root = ja_dir / root_rel
    main_rel = proj.rel(proj.main)
    for u in units:
        texts = [chunker.merge_translation(c.text, results[c.id].text) for c in u.chunks]
        text = u.assemble(texts)
        if u.rel == main_rel:
            text = latex.inject_japanese_support(text, localize_names=not args.keep_english_names)
        (ja_root / u.rel).write_text(text, encoding="utf-8")
    state_path.write_text(json.dumps({"root": root_rel, "main": main_rel}), encoding="utf-8")

    report = [
        {"id": r.chunk.id, "status": r.status, "warnings": r.warnings, "errors": r.errors}
        for u in units for c in u.chunks for r in [results[c.id]] if c.translate
    ]
    (workdir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    fallbacks = [r for r in report if r["status"] == "fallback"]
    warned = [r for r in report if r["warnings"]]
    if fallbacks:
        log(f"翻訳できず原文のまま残した断片が {len(fallbacks)} 個あります: "
            + ", ".join(r["id"] for r in fallbacks))
        log("  再実行するとこれらの断片だけを翻訳し直します．")
    if warned:
        log(f"\\ref / \\cite の不一致がある断片が {len(warned)} 個あります（詳細: {workdir / 'report.json'}）")

    src_zip = args.output_dir / f"{stem}_ja_src.zip"
    zip_dir(ja_root, src_zip)
    log(f"翻訳済みソース: {src_zip}")

    if args.no_build:
        return 0
    return build(args, ja_root, main_rel, stem)


if __name__ == "__main__":
    sys.exit(main())
