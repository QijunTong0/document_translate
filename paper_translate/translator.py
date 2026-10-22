"""チャンクごとの翻訳の実行（キャッシュ，並列実行，検証と再試行）．"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from . import prompts
from .chunker import Chunk, FileUnit
from .engines import Engine, EngineError
from .validate import validate

CONTEXT_CHARS = 1500
MAX_CONSECUTIVE_ENGINE_FAILURES = 3


class TranslationAborted(Exception):
    pass


@dataclass
class ChunkResult:
    chunk: Chunk
    status: str                      # translated / cached / skipped / fallback
    text: str
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s)


class Translator:
    def __init__(self, engine: Engine, guidelines: str, workdir: Path, *,
                 glossary: str = "", paper_context: str = "", jobs: int = 3,
                 retries: int = 2, use_cache: bool = True):
        self.engine = engine
        self.guidelines = guidelines
        self.glossary = glossary
        self.paper_context = paper_context
        self.jobs = max(1, jobs)
        self.retries = max(0, retries)
        self.use_cache = use_cache
        self.cache_dir = workdir / "cache"
        self.log_dir = workdir / "logs"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._consecutive_engine_failures = 0
        self._abort = threading.Event()

    # -- プロンプト ----------------------------------------------------------

    def _prompt(self, chunk: Chunk, position: str, preceding: str, feedback: str = "") -> str:
        return prompts.build_translation_prompt(
            self.guidelines, chunk.text,
            file_label=chunk.file, kind=chunk.kind, position=position,
            paper_context=self.paper_context, glossary=self.glossary,
            preceding=preceding, feedback=feedback,
        )

    def _cache_path(self, prompt: str) -> Path:
        key = hashlib.sha256((self.engine.identity() + "\n" + prompt).encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.json"

    # -- 1 チャンクの翻訳 ----------------------------------------------------

    def _record_engine_result(self, failed: bool) -> None:
        with self._lock:
            if failed:
                self._consecutive_engine_failures += 1
                if self._consecutive_engine_failures >= MAX_CONSECUTIVE_ENGINE_FAILURES:
                    self._abort.set()
            else:
                self._consecutive_engine_failures = 0

    def translate_chunk(self, chunk: Chunk, position: str, preceding: str) -> ChunkResult:
        if not chunk.translate:
            return ChunkResult(chunk, "skipped", chunk.text)

        base_prompt = self._prompt(chunk, position, preceding)
        cache = self._cache_path(base_prompt)
        if self.use_cache and cache.exists():
            data = json.loads(cache.read_text(encoding="utf-8"))
            return ChunkResult(chunk, "cached", data["translation"], data.get("warnings", []))

        feedback = ""
        errors: list[str] = []
        stem = self.log_dir / _safe_name(chunk.id)
        for attempt in range(self.retries + 1):
            if self._abort.is_set():
                raise TranslationAborted("翻訳エンジンの呼び出しが連続して失敗したため中断しました")
            prompt = self._prompt(chunk, position, preceding, feedback) if feedback else base_prompt
            Path(f"{stem}.{attempt}.prompt.txt").write_text(prompt, encoding="utf-8")
            try:
                response = self.engine.run(prompt)
            except EngineError as e:
                errors = [str(e)]
                self._record_engine_result(failed=True)
                log(f"  [{chunk.id}] エンジンの呼び出しに失敗（{attempt + 1} 回目）: {str(e).splitlines()[0]}")
                time.sleep(min(60, 5 * 2 ** attempt))
                continue
            self._record_engine_result(failed=False)
            Path(f"{stem}.{attempt}.response.txt").write_text(response, encoding="utf-8")

            try:
                translated = prompts.parse_translation(response)
            except prompts.ResponseFormatError as e:
                errors = [str(e)]
                feedback = f"- {e}．指定した出力形式を守ること．"
                log(f"  [{chunk.id}] 応答の形式が不正（{attempt + 1} 回目）: {e}")
                continue

            result = validate(chunk.text, translated)
            if result.ok:
                cache.write_text(json.dumps(
                    {"id": chunk.id, "translation": translated, "warnings": result.warnings},
                    ensure_ascii=False, indent=1), encoding="utf-8")
                return ChunkResult(chunk, "translated", translated, result.warnings)
            errors = result.errors
            feedback = result.feedback()
            log(f"  [{chunk.id}] 訳文の検査で問題を検出（{attempt + 1} 回目）: " + " / ".join(result.errors))

        # 再試行しても解決しない場合は，コンパイルを壊さないよう原文を残す
        return ChunkResult(chunk, "fallback", chunk.text, errors=errors)

    # -- 全体 ----------------------------------------------------------------

    def translate_all(self, units: list[FileUnit]) -> dict[str, ChunkResult]:
        ordered = [c for u in units for c in u.chunks]
        targets = [c for c in ordered if c.translate]
        total = len(targets)
        positions = {c.id: f"{i + 1}/{total}" for i, c in enumerate(targets)}
        preceding = {}
        for prev, cur in zip([None] + ordered[:-1], ordered):
            preceding[cur.id] = prev.text[-CONTEXT_CHARS:] if prev else ""

        results: dict[str, ChunkResult] = {}
        done = 0
        with ThreadPoolExecutor(max_workers=self.jobs) as pool:
            futures = {
                pool.submit(self.translate_chunk, c, positions.get(c.id, "-"), preceding[c.id]): c
                for c in ordered
            }
            try:
                for fut in as_completed(futures):
                    res = fut.result()
                    results[res.chunk.id] = res
                    if res.chunk.translate:
                        done += 1
                        log(f"[{done}/{total}] {res.chunk.id}: {res.status}")
            except TranslationAborted:
                self._abort.set()
                for f in futures:
                    f.cancel()
                raise
        return results


def generate_glossary(engine: Engine, instructions: str, units: list[FileUnit],
                      paper_context: str, user_glossary: str, max_chars: int,
                      log_dir: Path) -> prompts.Glossary:
    text = "".join(c.text for u in units for c in u.chunks if c.translate)
    if len(text) > max_chars:
        text = text[:max_chars]
    prompt = prompts.build_glossary_prompt(instructions, text, paper_context, user_glossary)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "glossary.prompt.txt").write_text(prompt, encoding="utf-8")
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = engine.run(prompt)
            (log_dir / "glossary.response.txt").write_text(response, encoding="utf-8")
            return prompts.parse_glossary(response)
        except (EngineError, prompts.ResponseFormatError) as e:
            last_error = e
            log(f"用語集の生成に失敗（{attempt + 1} 回目）: {str(e).splitlines()[0]}")
    raise EngineError(f"用語集を生成できませんでした: {last_error}")
