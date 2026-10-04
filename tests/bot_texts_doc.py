"""Разбор `docs/03-bot-texts.md`: тексты по умолчанию — ключ и текст.

Ключ в документе записан двумя способами: строкой таблицы «| `ключ` | текст |» или
строкой, которая начинается с `` `ключ` ``, а следом идёт блок кода с текстом.
"""

import re
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "03-bot-texts.md"

_TABLE_ROW = re.compile(r"^\|\s*`([a-z_][\w.]*)`\s*\|\s*(.*?)\s*\|\s*$")
_KEY_LINE = re.compile(r"^`([a-z_][\w.]*)`")
_FENCE = "```"


def parse(text: str) -> dict[str, str]:
    lines = text.splitlines()
    result: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        row = _TABLE_ROW.match(line)
        key_line = _KEY_LINE.match(line)
        if row:
            result[row.group(1)] = row.group(2)
        elif key_line and index + 1 < len(lines) and lines[index + 1] == _FENCE:
            end = lines.index(_FENCE, index + 2)
            result[key_line.group(1)] = "\n".join(lines[index + 2 : end])
            index = end
        index += 1
    return result


def default_texts() -> dict[str, str]:
    return parse(DOC.read_text(encoding="utf-8"))
