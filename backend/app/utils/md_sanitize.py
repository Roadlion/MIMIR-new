"""
backend/app/utils/md_sanitize.py

Server-side sanitizer for LLM-generated markdown.
Runs BEFORE the assistant message is saved to the database,
so every downstream consumer (frontend renderer, docx exporter)
receives clean, valid markdown.

Root causes being fixed:
  1. LLMs occasionally emit a lone '|' on its own line before or between table
     sections — this is not valid markdown and confuses parsers.
  2. LLMs frequently insert blank lines between the table header row and the
     GFM separator row (|:---|:---:|).  marked.js (and most parsers) require
     those two rows to be on consecutive lines with no gap.
  3. Rare '|::---' double-colon alignment glitch from certain models.
"""

import re


def sanitize_markdown(text: str) -> str:
    """Return a cleaned copy of LLM-generated markdown.

    Safe to call on any string; returns it unchanged if no issues are found.
    """
    if not text:
        return text

    # ── 1. Remove lines that contain ONLY a pipe (with optional surrounding whitespace)
    #       e.g. a bare '|' that the model emits as a table "opener"
    text = re.sub(r'(?m)^[ \t]*\|[ \t]*$', '', text)

    # ── 2. Collapse blank lines between a header row and its GFM separator row.
    #       Pattern:  | col1 | col2 |\n\n\n|:---|:---|\n
    #       After  :  | col1 | col2 |\n|:---|:---|\n
    #       We loop up to 4 times to handle multiple consecutive blank lines.
    _sep_pattern = re.compile(
        r'(\|[^\n]+\|)\n[ \t]*\n+'           # header row + 1+ blank lines
        r'(\|[ \t]*:?-+:?[ \t]*'             # separator row start: |:---|
        r'(?:\|[ \t]*:?-*:?[ \t]*)*\|)',     # remaining separator cells
        re.MULTILINE
    )
    for _ in range(4):
        text, n = _sep_pattern.subn(r'\1\n\2', text)
        if n == 0:
            break

    # ── 3. Fix double-colon alignment syntax from some model versions
    #       e.g.  |::---  →  |:---   and  ---::|  →  ---:|
    text = re.sub(r'\|::+', '|:', text)
    text = re.sub(r'::+\|', ':|', text)

    # ── 4. Collapse sequences of 3+ blank lines down to 2 (cosmetic)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text
