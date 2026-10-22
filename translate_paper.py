#!/usr/bin/env python3
"""英語論文の LaTeX ソースを日本語に翻訳して PDF を作成する．"""

import sys

from paper_translate.cli import main

if __name__ == "__main__":
    sys.exit(main())
