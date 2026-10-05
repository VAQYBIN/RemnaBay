"""Бренд оператора на раме RemnaBay (решение 0038): название, логотип, цвета."""

from remnabay.brand._assets import (
    MAX_ASSET_BYTES,
    PNG,
    SVG,
    AssetError,
    AssetKind,
    BrandAsset,
    asset_versions,
    delete_asset,
    get_asset,
    save_asset,
    validate_asset,
)
from remnabay.brand._settings import (
    BRAND_NAME,
    BRAND_PRIMARY_COLOR,
    BRAND_SECONDARY_COLOR,
    MAX_NAME_LENGTH,
    WELCOME_TEXT_KEY,
    brand_name,
)
from remnabay.brand._theme import (
    REMNABAY_PRIMARY,
    BrandPalette,
    Semantic,
    Theme,
    ThemePalette,
    build_palette,
)

__all__ = [
    "BRAND_NAME",
    "BRAND_PRIMARY_COLOR",
    "BRAND_SECONDARY_COLOR",
    "MAX_ASSET_BYTES",
    "MAX_NAME_LENGTH",
    "PNG",
    "REMNABAY_PRIMARY",
    "SVG",
    "WELCOME_TEXT_KEY",
    "AssetError",
    "AssetKind",
    "BrandAsset",
    "BrandPalette",
    "Semantic",
    "Theme",
    "ThemePalette",
    "asset_versions",
    "brand_name",
    "build_palette",
    "delete_asset",
    "get_asset",
    "save_asset",
    "validate_asset",
]
