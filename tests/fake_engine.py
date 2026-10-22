#!/usr/bin/env python3
"""テスト用の翻訳エンジン．

標準入力のプロンプトから翻訳対象を取り出し，文章の行の先頭に「【訳】」を付けて返す．
LaTeX の構造は変えないので，エンジン以外の処理の確認に使える．
"""

import re
import sys

prompt = sys.stdin.read()

if "<<<GLOSSARY_BEGIN>>>" in prompt:
    print("<<<GLOSSARY_BEGIN>>>")
    print("feature selection\t特徴選択\t")
    print("support recovery\tサポート復元\t")
    print("Lasso\tLasso\t固有名詞")
    print("<<<GLOSSARY_END>>>")
    sys.exit(0)

m = re.search(r"<<<SOURCE_BEGIN>>>\n(.*?)\n<<<SOURCE_END>>>", prompt, re.DOTALL)
lines = []
for line in m.group(1).split("\n"):
    if re.match(r"\s*[A-Za-z]", line):
        line = re.sub(r"^(\s*)", r"\1【訳】", line, count=1)
    lines.append(line)
print("<<<TRANSLATION_BEGIN>>>")
print("\n".join(lines))
print("<<<TRANSLATION_END>>>")
