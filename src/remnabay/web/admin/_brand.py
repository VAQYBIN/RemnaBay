"""Бренд оператора: публичный бренд для рамы и настройки «Бренд» (1.11, 1.23, 1.24)."""

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from remnabay.brand import (
    BRAND_NAME,
    BRAND_PRIMARY_COLOR,
    BRAND_SECONDARY_COLOR,
    MAX_ASSET_BYTES,
    MAX_NAME_LENGTH,
    WELCOME_TEXT_KEY,
    AssetError,
    AssetKind,
    BrandPalette,
    Semantic,
    asset_versions,
    brand_name,
    build_palette,
    delete_asset,
    save_asset,
)
from remnabay.brand._settings import Color
from remnabay.shop_settings import SHOP_LANGUAGE, get_setting, set_setting
from remnabay.texts import (
    TextError,
    get_override,
    load_default_catalogs,
    reset_override,
    save_override,
)
from remnabay.web._body import read_limited
from remnabay.web.admin._deps import DbSession, Owner
from remnabay.web.brand_files import asset_url

# Пока название не задано, рама показывает название RemnaBay (1.23, 0038)
REMNABAY_NAME = "RemnaBay"
MAX_WELCOME_LENGTH = 1000

public_router = APIRouter(tags=["brand"])
router = APIRouter(prefix="/settings/brand", tags=["settings"])


@lru_cache(maxsize=64)
def _palette(primary: str, secondary: str | None) -> BrandPalette:
    return build_palette(primary, secondary)


class TokensOut(BaseModel):
    """Токены оформления обеих тем: имя → значение CSS."""

    light: dict[str, str]
    dark: dict[str, str]


def _tokens(palette: BrandPalette) -> TokensOut:
    return TokensOut(light=palette.light.tokens, dark=palette.dark.tokens)


class BrandOut(BaseModel):
    """Бренд для рамы: экран входа и все экраны админки (1.23)."""

    name: str
    # Название не задано — показывается название RemnaBay
    name_is_default: bool
    # Нет квадратного знака — рама показывает знак RemnaBay
    mark_url: str | None
    logo_url: str | None
    tokens: TokensOut


async def _current_palette(session: DbSession) -> BrandPalette:
    primary = await get_setting(session, BRAND_PRIMARY_COLOR)
    secondary = await get_setting(session, BRAND_SECONDARY_COLOR)
    return _palette(primary, secondary)


@public_router.get("/brand")
async def brand(session: DbSession) -> BrandOut:
    """Без входа: экран входа оформлен брендом оператора (1.23)."""
    name = await brand_name(session)
    versions = await asset_versions(session)
    mark, logo = versions.get(AssetKind.MARK), versions.get(AssetKind.LOGO)
    return BrandOut(
        name=name or REMNABAY_NAME,
        name_is_default=not name,
        mark_url=asset_url(AssetKind.MARK, mark) if mark else None,
        logo_url=asset_url(AssetKind.LOGO, logo) if logo else None,
        tokens=_tokens(await _current_palette(session)),
    )


class PaletteOut(BaseModel):
    """Каким станет цвет оператора (1.24)."""

    light_primary: str
    dark_primary: str
    # Цвет пришлось подстроить ради контраста
    adjusted: bool
    # Основной цвет близок по тону к этим смысловым цветам — предупреждение
    warnings: list[Semantic]
    tokens: TokensOut


def _palette_out(palette: BrandPalette) -> PaletteOut:
    return PaletteOut(
        light_primary=palette.light.primary,
        dark_primary=palette.dark.primary,
        adjusted=palette.primary_adjusted,
        warnings=list(palette.semantic_warnings),
        tokens=_tokens(palette),
    )


class AssetOut(BaseModel):
    url: str


class BrandSettingsOut(BaseModel):
    name: str
    primary_color: str
    secondary_color: str | None
    welcome_text: str
    welcome_text_is_default: bool
    mark: AssetOut | None
    logo: AssetOut | None
    palette: PaletteOut


async def _welcome_language(session: DbSession) -> str:
    return await get_setting(session, SHOP_LANGUAGE)


async def _settings_out(session: DbSession) -> BrandSettingsOut:
    primary = await get_setting(session, BRAND_PRIMARY_COLOR)
    secondary = await get_setting(session, BRAND_SECONDARY_COLOR)
    language = await _welcome_language(session)
    override = await get_override(session, WELCOME_TEXT_KEY, language)
    default = load_default_catalogs()[language].texts[WELCOME_TEXT_KEY]
    versions = await asset_versions(session)
    mark, logo = versions.get(AssetKind.MARK), versions.get(AssetKind.LOGO)
    return BrandSettingsOut(
        name=await brand_name(session),
        primary_color=primary,
        secondary_color=secondary,
        welcome_text=override if override is not None else default,
        welcome_text_is_default=override is None,
        mark=AssetOut(url=asset_url(AssetKind.MARK, mark)) if mark else None,
        logo=AssetOut(url=asset_url(AssetKind.LOGO, logo)) if logo else None,
        palette=_palette_out(_palette(primary, secondary)),
    )


@router.get("")
async def brand_settings(session: DbSession, owner: Owner) -> BrandSettingsOut:
    del owner
    return await _settings_out(session)


type _Name = Annotated[str, Field(max_length=MAX_NAME_LENGTH)]
type _Welcome = Annotated[str, Field(max_length=MAX_WELCOME_LENGTH)]


class BrandSettingsIn(BaseModel):
    name: _Name
    primary_color: Color
    secondary_color: Color | None = None
    # Пусто — текст по умолчанию из 03-bot-texts.md
    welcome_text: _Welcome | None = None


@router.put("")
async def update_brand_settings(
    body: BrandSettingsIn, session: DbSession, owner: Owner
) -> BrandSettingsOut:
    """Изменения видны сразу, без перезапуска (1.23); каждое — в журнале (4.25)."""
    changes = [
        (BRAND_NAME, body.name.strip()),
        (BRAND_PRIMARY_COLOR, body.primary_color),
    ]
    for setting, value in changes:
        if await get_setting(session, setting) != value:
            await set_setting(session, setting, value, member_id=owner.id)
    if await get_setting(session, BRAND_SECONDARY_COLOR) != body.secondary_color:
        await set_setting(session, BRAND_SECONDARY_COLOR, body.secondary_color, member_id=owner.id)
    await _save_welcome(session, body.welcome_text, owner.id)
    await session.commit()
    return await _settings_out(session)


async def _save_welcome(session: DbSession, text: str | None, member_id: int) -> None:
    language = await _welcome_language(session)
    default = load_default_catalogs()[language].texts[WELCOME_TEXT_KEY]
    current = await get_override(session, WELCOME_TEXT_KEY, language)
    if text is None or not text.strip() or text.strip() == default:
        await reset_override(session, WELCOME_TEXT_KEY, language, member_id=member_id)
        return
    if text.strip() == current:
        return
    try:
        await save_override(session, WELCOME_TEXT_KEY, language, text.strip(), member_id=member_id)
    except TextError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error


class ColorsIn(BaseModel):
    primary_color: Color
    secondary_color: Color | None = None


@router.post("/preview")
async def preview_palette(body: ColorsIn, owner: Owner) -> PaletteOut:
    """Предпросмотр в обеих темах до сохранения (1.24)."""
    del owner
    return _palette_out(_palette(body.primary_color, body.secondary_color))


@router.put("/assets/{kind}")
async def upload_asset(
    kind: AssetKind, request: Request, session: DbSession, owner: Owner
) -> BrandSettingsOut:
    """Логотип — телом запроса с типом файла: image/svg+xml или image/png (0051)."""
    data = await read_limited(request, MAX_ASSET_BYTES)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    try:
        await save_asset(session, kind, content_type, data, member_id=owner.id)
    except AssetError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    await session.commit()
    return await _settings_out(session)


@router.delete("/assets/{kind}")
async def remove_asset(kind: AssetKind, session: DbSession, owner: Owner) -> BrandSettingsOut:
    await delete_asset(session, kind, member_id=owner.id)
    await session.commit()
    return await _settings_out(session)
