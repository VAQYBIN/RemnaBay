"""Текст рассылки: разметка Telegram (HTML) → текст и сущности форматирования (11.1).

Владелец пишет текст в разметке Telegram: кнопки над полем ставят теги вокруг
выделенного, рядом — предпросмотр (решение 0065, экран А8). Магазин сам разбирает
разметку и отправляет текст с сущностями: так длина считается до отправки, а
ошибка разметки видна владельцу сразу, а не в итогах рассылки.

Поддерживается то, что перечисляет 11.1: жирный, курсив, подчёркнутый,
зачёркнутый, скрытый текст, ссылка, цитата, моноширинный (строка и блок).
Из мнемоник — только `&lt;`, `&gt;`, `&amp;`, `&quot;` и числовые, как в Telegram.
"""

from dataclasses import dataclass
from html.parser import HTMLParser

from remnabay.messaging import TextEntity, utf16_length

# Ограничения Telegram: текст сообщения и подпись к изображению (11.1)
MAX_TEXT_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024

_SIMPLE_TAGS = {
    "b": "bold",
    "strong": "bold",
    "i": "italic",
    "em": "italic",
    "u": "underline",
    "ins": "underline",
    "s": "strikethrough",
    "strike": "strikethrough",
    "del": "strikethrough",
    "tg-spoiler": "spoiler",
    "code": "code",
    "pre": "pre",
    "blockquote": "blockquote",
}
_ENTITY_REFS = {"lt": "<", "gt": ">", "amp": "&", "quot": '"'}
_LINK_SCHEMES = ("https://", "http://", "tg://")
_SPOILER_CLASS = "tg-spoiler"
_LANGUAGE_PREFIX = "language-"


class FormatError(ValueError):
    """Разметка не подходит; текст ошибки показывается владельцу."""


@dataclass(frozen=True)
class FormattedText:
    text: str
    entities: tuple[TextEntity, ...]

    @property
    def length(self) -> int:
        """Длина без разметки — в единицах UTF-16, как в счётчике админки."""
        return utf16_length(self.text)


@dataclass
class _Open:
    tag: str
    type: str
    offset: int
    url: str | None = None
    language: str | None = None
    # `<code class="language-…">` сразу внутри `<pre>` — язык блока, а не своя сущность
    merged: bool = False


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.position = 0
        self.stack: list[_Open] = []
        self.entities: list[TextEntity] = []

    def _text(self, text: str) -> None:
        self.parts.append(text)
        self.position += utf16_length(text)

    def handle_data(self, data: str) -> None:
        self._text(data)

    def handle_entityref(self, name: str) -> None:
        self._text(_ENTITY_REFS.get(name, f"&{name};"))

    def handle_charref(self, name: str) -> None:
        try:
            code = int(name[1:], 16) if name[:1] in ("x", "X") else int(name)
            self._text(chr(code))
        except ValueError, OverflowError:
            raise FormatError(f"Неверный код символа «&#{name};»") from None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value for name, value in attrs}
        if tag == "a":
            url = (values.get("href") or "").strip()
            if not url.startswith(_LINK_SCHEMES):
                raise FormatError(
                    f"Ссылка должна начинаться с https://, http:// или tg://: «{url}»"
                )
            self.stack.append(_Open(tag, "text_link", self.position, url=url))
            return
        if tag == "span":
            if values.get("class") != _SPOILER_CLASS:
                raise FormatError('Тег <span> поддерживается только как <span class="tg-spoiler">')
            self.stack.append(_Open(tag, "spoiler", self.position))
            return
        kind = _SIMPLE_TAGS.get(tag)
        if kind is None:
            raise FormatError(f"Тег <{tag}> не поддерживается")
        if tag == "blockquote" and "expandable" in values:
            kind = "expandable_blockquote"
        opened = _Open(tag, kind, self.position)
        language = values.get("class") or ""
        parent = self.stack[-1] if self.stack else None
        if (
            tag == "code"
            and parent is not None
            and parent.tag == "pre"
            and parent.offset == self.position
            and language.startswith(_LANGUAGE_PREFIX)
        ):
            parent.language = language.removeprefix(_LANGUAGE_PREFIX)
            opened.merged = True
        self.stack.append(opened)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raise FormatError(f"Тег <{tag}/> не поддерживается: перенос строки — обычный Enter")

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1].tag != tag:
            expected = f"</{self.stack[-1].tag}>" if self.stack else "открывающего тега"
            raise FormatError(f"Лишний или не на месте </{tag}>: ожидался {expected}")
        opened = self.stack.pop()
        length = self.position - opened.offset
        if length > 0 and not opened.merged:
            self.entities.append(
                TextEntity(
                    opened.type,
                    opened.offset,
                    length,
                    url=opened.url,
                    language=opened.language,
                )
            )

    def handle_comment(self, data: str) -> None:
        raise FormatError("Комментарии <!-- --> в тексте не поддерживаются")

    def handle_decl(self, decl: str) -> None:
        raise FormatError(f"Конструкция <!{decl}> не поддерживается")

    def handle_pi(self, data: str) -> None:
        raise FormatError(f"Конструкция <?{data}> не поддерживается")

    def unknown_decl(self, data: str) -> None:
        raise FormatError(f"Конструкция <![{data}]> не поддерживается")


def parse(markup: str) -> FormattedText:
    """Разбирает разметку. Пробелы и переносы по краям отбрасываются — их отбросил бы
    и Telegram, сдвинув сущности."""
    parser = _Parser()
    parser.feed(markup.strip())
    parser.close()
    if parser.stack:
        raise FormatError(f"Не закрыт тег <{parser.stack[-1].tag}>")
    entities = sorted(parser.entities, key=lambda entity: (entity.offset, -entity.length))
    return FormattedText("".join(parser.parts), tuple(entities))


def check_length(text: FormattedText, *, with_image: bool) -> None:
    """Текст — до 4096 знаков, с изображением — до 1024 (подпись, 11.1)."""
    limit = MAX_CAPTION_LENGTH if with_image else MAX_TEXT_LENGTH
    if text.length > limit:
        where = "С изображением текст" if with_image else "Текст"
        raise FormatError(f"{where} — не больше {limit} знаков, сейчас {text.length}")
