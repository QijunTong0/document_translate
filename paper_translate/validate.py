"""訳文が LaTeX ソースとして原文の構造を保っているかの検査．"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from . import texutil


@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)    # 再翻訳が必要な問題
    warnings: list[str] = field(default_factory=list)  # 記録だけする問題

    @property
    def ok(self) -> bool:
        return not self.errors

    def feedback(self) -> str:
        return "\n".join(f"- {m}" for m in self.errors + self.warnings)


def _diff(a: Counter, b: Counter) -> str:
    missing = a - b
    added = b - a
    parts = []
    if missing:
        parts.append("欠落: " + ", ".join(f"{k}×{v}" for k, v in sorted(missing.items(), key=str)))
    if added:
        parts.append("追加: " + ", ".join(f"{k}×{v}" for k, v in sorted(added.items(), key=str)))
    return "; ".join(parts)


def validate(source: str, translated: str) -> ValidationResult:
    r = ValidationResult()

    src_ph = Counter(texutil.PLACEHOLDER_RE.findall(source))
    out_ph = Counter(texutil.PLACEHOLDER_RE.findall(translated))
    if src_ph != out_ph:
        r.errors.append("保護領域の目印 [[[KEEP_xxxx]]] が変化している（"
                        + _diff(src_ph, out_ph) + "）．目印は元の位置にそのまま残すこと．")

    src_env, out_env = texutil.env_counter(source), texutil.env_counter(translated)
    if src_env != out_env:
        fmt = lambda c: Counter({f"\\{k}{{{n}}}": v for (k, n), v in c.items()})
        r.errors.append("\\begin / \\end の構成が原文と異なる（" + _diff(fmt(src_env), fmt(out_env)) + "）．")

    if texutil.brace_balance(source) != texutil.brace_balance(translated):
        r.errors.append("波括弧 { } の対応が原文と異なる．")

    if texutil.dollar_count(source) != texutil.dollar_count(translated):
        r.errors.append("数式記号 $ の数が原文と異なる．数式は変更しないこと．")

    src_labels, out_labels = texutil.key_counter(source, "label"), texutil.key_counter(translated, "label")
    if src_labels != out_labels:
        r.errors.append("\\label のキーが原文と異なる（" + _diff(src_labels, out_labels) + "）．")

    for kind, name in (("ref", "\\ref 系コマンド"), ("cite", "\\cite 系コマンド")):
        a, b = texutil.key_counter(source, kind), texutil.key_counter(translated, kind)
        if set(a) != set(b):
            r.warnings.append(f"{name}のキーが原文と異なる（" + _diff(a, b) + "）．")

    words = texutil.english_word_count(source)
    if words >= 15 and texutil.japanese_char_count(translated) == 0:
        r.errors.append("日本語に翻訳されていない．")
    if words >= 50 and len(translated) < 0.25 * len(source):
        r.errors.append("訳文が原文に比べて極端に短い．省略せずに全体を訳すこと．")
    return r
