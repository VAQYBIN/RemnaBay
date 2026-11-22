"""Разбор `docs/03-bot-texts.md`: тексты по умолчанию — ключ и текст.

Ключ в документе записан двумя способами: строкой таблицы «| `ключ` | текст |» или
строкой, которая начинается с `` `ключ` ``, а следом идёт блок кода с текстом.
"""

import re
from dataclasses import dataclass
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "03-bot-texts.md"

_TABLE_ROW = re.compile(r"^\|\s*`([a-z_][\w.]*)`\s*\|\s*(.*?)\s*\|\s*$")
_KEY_LINE = re.compile(r"^`([a-z_][\w.]*)`")
_FENCE = "```"
# Единица с формами множественного числа: «- **`unit.days`** — …»
_UNIT_ITEM = re.compile(r"^- \*\*`([a-z_][\w.]*)`\*\*")


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


@dataclass(frozen=True)
class Heading:
    """Раздел документа, в котором записан текст: заголовок и раздел над ним."""

    title: str
    # Заголовок второго уровня над заголовком третьего («События» над «С1…»)
    section: str | None


def headings(text: str) -> dict[str, Heading]:
    """Ключ → раздел (`##` или `###`), где текст записан: строкой таблицы, строкой
    перед блоком кода или пунктом списка единиц. Упоминания в пояснениях не считаются."""
    lines = text.splitlines()
    result: dict[str, Heading] = {}
    top: str | None = None
    current: Heading | None = None
    for index, line in enumerate(lines):
        if line.startswith("## "):
            top = line.removeprefix("## ").strip()
            current = Heading(top, None)
            continue
        if line.startswith("### "):
            current = Heading(line.removeprefix("### ").strip(), top)
            continue
        row = _TABLE_ROW.match(line)
        key_line = _KEY_LINE.match(line)
        unit = _UNIT_ITEM.match(line)
        if row:
            key = row.group(1)
        elif key_line and index + 1 < len(lines) and lines[index + 1] == _FENCE:
            key = key_line.group(1)
        elif unit:
            key = unit.group(1)
        else:
            continue
        if current is not None:
            result[key] = current
    return result


def default_headings() -> dict[str, Heading]:
    return headings(DOC.read_text(encoding="utf-8"))
