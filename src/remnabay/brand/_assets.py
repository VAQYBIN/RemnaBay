"""Логотипы оператора: квадратный знак и горизонтальный логотип (1.11, решения 0038, 0051).

Хранятся в базе: одна резервная копия и переезд без отдельной папки (0051).
Проверки при загрузке: SVG или PNG не больше 1 МБ; квадратный знак — квадрат,
PNG — не меньше 512×512; у PNG — прозрачный фон (есть альфа-канал).
"""

import hashlib
import re
import struct
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from remnabay.db import Base, str_enum_type
from remnabay.journal import Actor, Outcome, Subject, record

PNG = "image/png"
SVG = "image/svg+xml"
MAX_ASSET_BYTES = 1024 * 1024
MIN_MARK_PIXELS = 512
# Квадрат с точностью до 1 %: у векторного знака размеры бывают дробными
_SQUARE_TOLERANCE = 0.01
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Подпись, длина и тип чанка IHDR, 13 байт его данных, CRC
_PNG_MIN_BYTES = 33
# Цветовые типы PNG с альфа-каналом: оттенки серого + альфа, RGBA
_PNG_ALPHA_TYPES = frozenset({4, 6})
_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
JOURNAL_SUBJECT = "brand_asset"


class AssetKind(StrEnum):
    # Квадратный знак: обязателен (чек-лист 1.11), предлагается как аватар бота
    MARK = "mark"
    # Горизонтальный логотип: по желанию
    LOGO = "logo"


class AssetError(Exception):
    """Файл не подходит; текст ошибки показывается оператору."""


class BrandAsset(Base):
    __tablename__ = "brand_assets"

    kind: Mapped[AssetKind] = mapped_column(
        str_enum_type(AssetKind, "brand_asset_kind"), primary_key=True
    )
    content_type: Mapped[str] = mapped_column(String(32))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    # SHA-256 содержимого: адрес файла с ним кэшируется браузером до смены логотипа
    sha256: Mapped[str] = mapped_column(String(64))
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("team_members.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


@dataclass(frozen=True)
class _Size:
    width: float
    height: float

    @property
    def square(self) -> bool:
        largest = max(self.width, self.height)
        return largest > 0 and abs(self.width - self.height) <= largest * _SQUARE_TOLERANCE


def _png_size(data: bytes) -> tuple[_Size, bool]:
    """Размер PNG и есть ли прозрачность: по заголовку IHDR и чанку tRNS."""
    if len(data) < _PNG_MIN_BYTES or not data.startswith(_PNG_SIGNATURE) or data[12:16] != b"IHDR":
        raise AssetError("Файл не похож на PNG")
    width, height, _depth, color_type = struct.unpack(">IIBB", data[16:26])
    transparent = color_type in _PNG_ALPHA_TYPES
    position = len(_PNG_SIGNATURE)
    while not transparent and position + 8 <= len(data):
        length, chunk = struct.unpack(">I4s", data[position : position + 8])
        if chunk == b"tRNS":
            transparent = True
        if chunk == b"IDAT":
            break
        position += 12 + length
    return _Size(width, height), transparent


def _svg_length(value: str | None) -> float | None:
    if value is None or value.strip().endswith("%"):
        return None
    match = _NUMBER.match(value.strip())
    return float(match.group()) if match else None


def _svg_size(data: bytes) -> _Size:
    """Размер SVG по viewBox, иначе по width и height."""
    lowered = data.lower()
    # Определения сущностей не нужны логотипу и опасны для разбора XML
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise AssetError("SVG не должен содержать DOCTYPE и ENTITY")
    try:
        root = ElementTree.fromstring(data)  # noqa: S314 — DOCTYPE и ENTITY отклонены выше
    except ElementTree.ParseError as error:
        raise AssetError("Файл не похож на SVG") from error
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise AssetError("Файл не похож на SVG")
    view_box = root.get("viewBox")
    if view_box:
        numbers = [float(n) for n in _NUMBER.findall(view_box)]
        if len(numbers) == 4:
            return _Size(numbers[2], numbers[3])
    width, height = _svg_length(root.get("width")), _svg_length(root.get("height"))
    if width is None or height is None:
        raise AssetError("У SVG не указаны размеры: нужен viewBox")
    return _Size(width, height)


def validate_asset(kind: AssetKind, content_type: str, data: bytes) -> None:
    if len(data) > MAX_ASSET_BYTES:
        raise AssetError("Файл больше 1 МБ")
    if content_type == PNG:
        size, transparent = _png_size(data)
        if not transparent:
            raise AssetError("Нужен PNG с прозрачным фоном")
        if kind == AssetKind.MARK and min(size.width, size.height) < MIN_MARK_PIXELS:
            raise AssetError("Квадратный знак — не меньше 512×512 пикселей")
    elif content_type == SVG:
        size = _svg_size(data)
    else:
        raise AssetError("Логотип — SVG или PNG")
    if kind == AssetKind.MARK and not size.square:
        raise AssetError("Знак должен быть квадратным")


async def save_asset(
    session: AsyncSession, kind: AssetKind, content_type: str, data: bytes, *, member_id: int
) -> str:
    """Сохраняет логотип после проверки; возвращает хэш содержимого."""
    validate_asset(kind, content_type, data)
    digest = hashlib.sha256(data).hexdigest()
    values = {
        "content_type": content_type,
        "data": data,
        "sha256": digest,
        "updated_by_id": member_id,
    }
    await session.execute(
        insert(BrandAsset)
        .values(kind=kind, **values)
        .on_conflict_do_update(index_elements=[BrandAsset.kind], set_=values)
    )
    await record(
        session,
        actor=Actor.team_member(member_id),
        action="brand.asset_changed",
        outcome=Outcome.SUCCESS,
        subject=Subject(JOURNAL_SUBJECT, kind.value),
        details={"kind": kind.value, "content_type": content_type, "sha256": digest},
    )
    return digest


async def delete_asset(session: AsyncSession, kind: AssetKind, *, member_id: int) -> None:
    deleted = await session.scalar(
        delete(BrandAsset).where(BrandAsset.kind == kind).returning(BrandAsset.kind)
    )
    if deleted is None:
        return
    await record(
        session,
        actor=Actor.team_member(member_id),
        action="brand.asset_deleted",
        outcome=Outcome.SUCCESS,
        subject=Subject(JOURNAL_SUBJECT, kind.value),
        details={"kind": kind.value},
    )


async def get_asset(session: AsyncSession, kind: AssetKind) -> BrandAsset | None:
    return await session.get(BrandAsset, kind)


async def asset_versions(session: AsyncSession) -> dict[AssetKind, str]:
    """Хэши загруженных логотипов — без самих файлов."""
    rows = await session.execute(select(BrandAsset.kind, BrandAsset.sha256))
    return {kind: digest for kind, digest in rows}
