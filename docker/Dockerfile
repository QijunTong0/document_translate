# 日本語論文をコンパイルするための TeX Live 環境（LuaLaTeX + LuaTeX-ja）
ARG TEXLIVE_IMAGE=texlive/texlive:latest
FROM ${TEXLIVE_IMAGE}

# 任意の UID で実行してもフォントキャッシュを共有できるよう，キャッシュ置き場を固定する
ENV TEXMFVAR=/opt/texmf-var \
    HOME=/tmp

# luaotfload のフォントデータベースと LuaTeX-ja のキャッシュを事前に作っておく
RUN mkdir -p /opt/texmf-var \
 && luaotfload-tool --update \
 && cd /tmp \
 && printf '%s\n' '\documentclass{article}' '\usepackage{luatexja}' \
      '\begin{document}日本語 English \textbf{太字}\end{document}' > warmup.tex \
 && lualatex -interaction=nonstopmode warmup.tex \
 && rm -f warmup.* \
 && chmod -R a+rwX /opt/texmf-var

WORKDIR /work
