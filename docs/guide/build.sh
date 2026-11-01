#!/usr/bin/env bash
# Build the technical guide PDF from the LaTeX sources with pandoc.
set -euo pipefail
cd "$(dirname "$0")"
pandoc technical-guide.tex \
  --from latex \
  --pdf-engine=xelatex \
  --toc --toc-depth=2 \
  --number-sections -V secnumdepth=2 \
  --highlight-style=tango \
  -V geometry:margin=1in \
  -V colorlinks=true -V linkcolor=blue -V urlcolor=blue -V toccolor=black \
  -V fontsize=11pt \
  -o technical-guide.pdf
echo "built $(pwd)/technical-guide.pdf"
