"""翻訳エンジン（Claude Code / Codex の非対話モード，任意のコマンド）の呼び出し．"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path


class EngineError(Exception):
    pass


class Engine:
    name = "engine"

    def __init__(self, model: str | None = None, extra_args: list[str] | None = None,
                 timeout: float = 1200):
        self.model = model
        self.extra_args = extra_args or []
        self.timeout = timeout

    def identity(self) -> str:
        """キャッシュのキーに含める，エンジンと設定の識別子．"""
        return f"{self.name}|{self.model or ''}|{' '.join(self.extra_args)}"

    def check(self) -> None:
        pass

    def run(self, prompt: str) -> str:
        raise NotImplementedError

    def _exec(self, cmd: list[str], prompt: str, cwd: str) -> subprocess.CompletedProcess:
        try:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                cwd=cwd, timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise EngineError(f"{self.name} が {self.timeout:.0f} 秒以内に終了しませんでした") from e
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[-2000:]
            raise EngineError(f"{self.name} が終了コード {proc.returncode} で失敗しました:\n{detail}")
        return proc


def _require(binary: str) -> str:
    path = shutil.which(binary)
    if not path:
        raise EngineError(f"コマンド {binary} が見つかりません．インストールしてログインしてください．")
    return path


class ClaudeCodeEngine(Engine):
    """`claude -p` による非対話実行．ツールを無効にし，テキスト応答だけを得る．"""

    name = "claude"

    def __init__(self, binary: str = "claude", **kw):
        super().__init__(**kw)
        self.binary = binary

    def check(self) -> None:
        _require(self.binary)

    def run(self, prompt: str) -> str:
        cmd = [self.binary, "-p", "--output-format", "text", "--tools", "",
               "--no-session-persistence"]
        if self.model:
            cmd += ["--model", self.model]
        cmd += self.extra_args
        # プロジェクトの CLAUDE.md などを読み込ませないよう，空のディレクトリで実行する
        with tempfile.TemporaryDirectory(prefix="ptrans-claude-") as cwd:
            return self._exec(cmd, prompt, cwd).stdout


class CodexEngine(Engine):
    """`codex exec` による非対話実行．最終応答をファイルで受け取る．"""

    name = "codex"

    def __init__(self, binary: str = "codex", **kw):
        super().__init__(**kw)
        self.binary = binary

    def check(self) -> None:
        _require(self.binary)

    def run(self, prompt: str) -> str:
        with tempfile.TemporaryDirectory(prefix="ptrans-codex-") as cwd:
            out = Path(cwd) / "last_message.txt"
            cmd = [self.binary, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                   "--ephemeral", "--color", "never", "-C", cwd, "-o", str(out)]
            if self.model:
                cmd += ["-m", self.model]
            cmd += self.extra_args
            cmd.append("-")  # プロンプトを標準入力から読ませる
            proc = self._exec(cmd, prompt, cwd)
            if out.exists():
                return out.read_text(encoding="utf-8")
            return proc.stdout


class CommandEngine(Engine):
    """プロンプトを標準入力で受け取り，応答を標準出力に書く任意のコマンド．"""

    name = "command"

    def __init__(self, command: str, **kw):
        super().__init__(**kw)
        self.command = command

    def identity(self) -> str:
        return f"command|{self.command}"

    def check(self) -> None:
        _require(shlex.split(self.command)[0])

    def run(self, prompt: str) -> str:
        return self._exec(shlex.split(self.command), prompt, os.getcwd()).stdout


def create_engine(name: str, model: str | None = None, extra_args: list[str] | None = None,
                  command: str | None = None, timeout: float = 1200) -> Engine:
    if name == "claude":
        return ClaudeCodeEngine(model=model, extra_args=extra_args, timeout=timeout)
    if name == "codex":
        return CodexEngine(model=model, extra_args=extra_args, timeout=timeout)
    if name == "command":
        if not command:
            raise EngineError("--engine command には --engine-cmd が必要です")
        return CommandEngine(command, timeout=timeout)
    raise EngineError(f"未知のエンジンです: {name}")
