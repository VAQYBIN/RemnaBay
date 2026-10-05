"""Токены оформления обеих тем из цвета оператора (1.24, решение 0038, DESIGN.md).

Рама гарантирует читаемость, а не оператор: каждый цвет текста проверяется по
WCAG 2.2 AA (4,5:1) на всех поверхностях, где он может оказаться, — на стекле
каждого уровня поверх фона и поверх цветных пятен «воды» («Правило прочной
воды»). Элементы интерфейса — 3:1. Если цвет не проходит, меняется светлота
(и насыщенность, если её не показывает sRGB), а тон остаётся прежним.

Смысловые цвета (успех, внимание, ошибка) от бренда не зависят: они считаются
по нейтральным поверхностям с запасом, поэтому проходят на любых.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from functools import cache

from remnabay.brand._color import (
    Oklch,
    Rgb,
    composite,
    contrast,
    hue_distance,
    min_contrast,
    nearest_lightness,
    rgb_to_oklch,
)

# Пороги WCAG 2.2 AA (1.24)
TEXT_CONTRAST = 4.5
UI_CONTRAST = 3.0
# Запас для смысловых цветов: считаются по нейтральным поверхностям, а работают
# на подкрашенных брендом
_SEMANTIC_MARGIN = 0.4
# Близость тона к смысловому цвету, при которой админка предупреждает (1.24)
SEMANTIC_HUE_WARNING = 30.0
# Ниже этой насыщенности цвет почти серый — тон не читается, предупреждать не о чем
_HUELESS_CHROMA = 0.05

REMNABAY_PRIMARY = "#1FA4A0"
REMNABAY_SECONDARY = "#E9B872"

SCALE_STEPS = (50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950)
_SCALE_LIGHTNESS = (0.97, 0.94, 0.88, 0.80, 0.71, 0.62, 0.53, 0.45, 0.37, 0.29, 0.22)
_SCALE_CHROMA = (0.30, 0.45, 0.65, 0.85, 1.0, 1.0, 1.0, 0.90, 0.75, 0.60, 0.50)

_WHITE = Rgb(1.0, 1.0, 1.0)


class Theme(StrEnum):
    LIGHT = "light"
    DARK = "dark"


class Semantic(StrEnum):
    SUCCESS = "success"
    WARNING = "warning"
    DANGER = "danger"


# Исходные смысловые цвета; итоговые светлоты подбираются под контраст в каждой теме
_SEMANTIC_START = {
    Semantic.SUCCESS: Oklch(0.62, 0.15, 150.0),
    Semantic.WARNING: Oklch(0.76, 0.15, 75.0),
    Semantic.DANGER: Oklch(0.60, 0.20, 27.0),
}


@dataclass(frozen=True)
class _Glass:
    """Уровень стекла: цвет и непрозрачность (DESIGN.md, «Три уровня стекла»)."""

    color: Oklch
    alpha: float


@dataclass(frozen=True)
class _FrameTheme:
    """Нейтральная часть рамы одной темы: светлоты и плотность стекла."""

    background: float
    glow: float
    base: _Glass
    module: _Glass
    floating: _Glass
    text: float
    muted: float
    # Тёмный текст на светлой кнопке
    ink: float
    soft_alpha: float
    # Насыщенность подкраски нейтральных тоном бренда
    tint: float


_FRAME = {
    # «Глубокая вода»: тёмный фон, подкрашенный тоном бренда; стекло — тёмное матовое
    Theme.DARK: _FrameTheme(
        background=0.17,
        glow=0.34,
        base=_Glass(Oklch(0.21, 0.0, 0.0), 0.62),
        module=_Glass(Oklch(0.24, 0.0, 0.0), 0.78),
        floating=_Glass(Oklch(0.27, 0.0, 0.0), 0.94),
        text=0.96,
        muted=0.76,
        ink=0.18,
        soft_alpha=0.20,
        tint=0.02,
    ),
    # «Мелководье»: светлый прохладный фон; стекло — белое матовое, плотнее
    Theme.LIGHT: _FrameTheme(
        background=0.965,
        glow=0.86,
        base=_Glass(Oklch(1.0, 0.0, 0.0), 0.50),
        module=_Glass(Oklch(1.0, 0.0, 0.0), 0.74),
        floating=_Glass(Oklch(1.0, 0.0, 0.0), 0.90),
        text=0.22,
        muted=0.46,
        ink=0.20,
        soft_alpha=0.14,
        tint=0.018,
    ),
}


@dataclass(frozen=True)
class ThemePalette:
    """Токены одной темы: имя → значение CSS."""

    tokens: dict[str, str]
    # Каким стал основной цвет кнопки в этой теме
    primary: str


@dataclass(frozen=True)
class BrandPalette:
    light: ThemePalette
    dark: ThemePalette
    scale: dict[int, str]
    # Цвет оператора пришлось подстроить ради контраста хотя бы в одной теме
    primary_adjusted: bool
    # Смысловые цвета, к которым основной цвет близок по тону
    semantic_warnings: tuple[Semantic, ...]

    def theme(self, theme: Theme) -> ThemePalette:
        return self.light if theme == Theme.LIGHT else self.dark


def _glass_rgb(layer: _Glass, hue: float, tint: float) -> Rgb:
    return Oklch(layer.color.l, tint, hue).with_lightness(layer.color.l).to_rgb()


def _reading_backgrounds(
    frame: _FrameTheme, background: Rgb, glows: Sequence[Rgb], hue: float, tint: float
) -> list[Rgb]:
    """Где может оказаться текст: на стекле каждого уровня над фоном и над пятнами."""
    layers = [frame.base, frame.module, frame.floating]
    result: list[Rgb] = []
    for layer in layers:
        glass = _glass_rgb(layer, hue, tint)
        result.extend(composite(glass, layer.alpha, under) for under in (background, *glows))
    return result


def _on_color(fill: Rgb, ink: Rgb) -> Rgb | None:
    """Текст на заливке: белый или почти чёрный — что даёт 4,5:1."""
    options = [(contrast(fill, color), color) for color in (_WHITE, ink)]
    best_contrast, best = max(options, key=lambda option: option[0])
    return best if best_contrast >= TEXT_CONTRAST else None


def _fill(start: Oklch, backgrounds: Sequence[Rgb], ink: Rgb, margin: float = 0.0) -> Oklch:
    """Заливка кнопки или бейджа: 3:1 к поверхностям и читаемый текст на ней."""

    def passes(color: Rgb) -> bool:
        return (
            min_contrast(color, backgrounds) >= UI_CONTRAST + margin
            and _on_color_with_margin(color, ink, margin) is not None
        )

    return nearest_lightness(start, passes) or start


def _on_color_with_margin(fill: Rgb, ink: Rgb, margin: float) -> Rgb | None:
    options = [(contrast(fill, color), color) for color in (_WHITE, ink)]
    best_contrast, best = max(options, key=lambda option: option[0])
    return best if best_contrast >= TEXT_CONTRAST + margin else None


def _text(start: Oklch, backgrounds: Sequence[Rgb], margin: float = 0.0) -> Oklch:
    """Цвет текста или ссылки: 4,5:1 ко всем поверхностям."""
    found = nearest_lightness(
        start, lambda color: min_contrast(color, backgrounds) >= TEXT_CONTRAST + margin
    )
    return found or start


def scale(primary: Oklch) -> dict[int, str]:
    """Шкала из 11 ступеней: тон фиксирован, меняются светлота и насыщенность."""
    return {
        step: Oklch(lightness, primary.c * factor, primary.h)
        .with_lightness(lightness)
        .to_rgb()
        .to_hex()
        for step, lightness, factor in zip(
            SCALE_STEPS, _SCALE_LIGHTNESS, _SCALE_CHROMA, strict=True
        )
    }


@cache
def _semantic_tokens(theme: Theme) -> dict[str, str]:
    """Смысловые цвета рамы — одинаковые для всех магазинов (0038)."""
    frame = _FRAME[theme]
    neutral_background = Oklch(frame.background, 0.0, 0.0).to_rgb()
    neutral_glow = Oklch(frame.glow, 0.0, 0.0).to_rgb()
    backgrounds = _reading_backgrounds(frame, neutral_background, [neutral_glow], 0.0, 0.0)
    ink = Oklch(frame.ink, 0.0, 0.0).to_rgb()
    tokens: dict[str, str] = {}
    for name, start in _SEMANTIC_START.items():
        fill = _fill(start, backgrounds, ink, _SEMANTIC_MARGIN).to_rgb()
        on = _on_color(fill, ink) or _WHITE
        text = _text(start, backgrounds, _SEMANTIC_MARGIN).to_rgb()
        tokens[name.value] = fill.to_hex()
        tokens[f"on-{name.value}"] = on.to_hex()
        tokens[f"{name.value}-text"] = text.to_hex()
        tokens[f"{name.value}-soft"] = start.to_rgb().css(frame.soft_alpha)
    return tokens


def _theme_palette(
    theme: Theme, primary: Oklch, accent: Oklch, primary_scale: dict[int, str]
) -> ThemePalette:
    frame = _FRAME[theme]
    hue = primary.h
    tint = min(frame.tint, primary.c)
    background = Oklch(frame.background, tint, hue).with_lightness(frame.background).to_rgb()
    glow_primary = Oklch(frame.glow, min(primary.c, 0.10), hue).with_lightness(frame.glow)
    glow_accent = Oklch(frame.glow, min(accent.c, 0.10), accent.h).with_lightness(frame.glow)
    glows = [glow_primary.to_rgb(), glow_accent.to_rgb()]
    backgrounds = _reading_backgrounds(frame, background, glows, hue, tint)
    ink = Oklch(frame.ink, tint, hue).with_lightness(frame.ink).to_rgb()

    text = _text(Oklch(frame.text, tint, hue), backgrounds).to_rgb()
    muted = _text(Oklch(frame.muted, tint, hue), backgrounds).to_rgb()

    fill = _fill(primary, backgrounds, ink)
    fill_rgb = fill.to_rgb()
    on_primary = _on_color(fill_rgb, ink) or _WHITE
    darker = on_primary == _WHITE
    hover = fill.with_lightness(fill.l - 0.04 if darker else fill.l + 0.04).to_rgb()
    active = fill.with_lightness(fill.l - 0.08 if darker else fill.l + 0.08).to_rgb()
    soft_backgrounds = [
        composite(primary.to_rgb(), frame.soft_alpha, under) for under in backgrounds
    ]
    primary_text = _text(primary, [*backgrounds, *soft_backgrounds]).to_rgb()

    accent_fill = _fill(accent, backgrounds, ink).to_rgb()
    accent_text = _text(accent, backgrounds).to_rgb()

    glass = {
        name: Oklch(layer.color.l, tint, hue)
        .with_lightness(layer.color.l)
        .to_rgb()
        .css(layer.alpha)
        for name, layer in (
            ("glass-base", frame.base),
            ("glass-module", frame.module),
            ("glass-float", frame.floating),
        )
    }
    tokens = {
        "bg": background.to_hex(),
        "glow-1": glows[0].css(0.55 if theme == Theme.DARK else 0.70),
        "glow-2": glows[1].css(0.40 if theme == Theme.DARK else 0.55),
        **glass,
        "glass-edge": _WHITE.css(0.14 if theme == Theme.DARK else 0.70),
        "border": text.css(0.12),
        "text": text.to_hex(),
        "text-muted": muted.to_hex(),
        "primary": fill_rgb.to_hex(),
        "on-primary": on_primary.to_hex(),
        "primary-hover": hover.to_hex(),
        "primary-active": active.to_hex(),
        "primary-soft": primary.to_rgb().css(frame.soft_alpha),
        "primary-text": primary_text.to_hex(),
        "focus": primary_text.to_hex(),
        "accent": accent_fill.to_hex(),
        "on-accent": (_on_color(accent_fill, ink) or _WHITE).to_hex(),
        "accent-soft": accent.to_rgb().css(frame.soft_alpha),
        "accent-text": accent_text.to_hex(),
        **_semantic_tokens(theme),
        **{f"primary-{step}": value for step, value in primary_scale.items()},
    }
    return ThemePalette(tokens=tokens, primary=fill_rgb.to_hex())


def semantic_warnings(primary: Oklch) -> tuple[Semantic, ...]:
    """Смысловые цвета, к которым основной цвет близок по тону (1.24): предупреждение
    при сохранении, но не запрет."""
    if primary.c < _HUELESS_CHROMA:
        return ()
    return tuple(
        name
        for name, start in _SEMANTIC_START.items()
        if hue_distance(primary.h, start.h) < SEMANTIC_HUE_WARNING
    )


def build_palette(primary_hex: str, secondary_hex: str | None) -> BrandPalette:
    """Токены обеих тем из цветов оператора (1.24).

    Без дополнительного цвета акценты берутся из шкалы основного, а не из палитры
    RemnaBay: иначе чужой песочный цвет смешался бы с цветом оператора (0038).
    """
    primary = rgb_to_oklch(Rgb.from_hex(primary_hex))
    primary_scale = scale(primary)
    light_accent = dark_accent = None
    if secondary_hex:
        light_accent = dark_accent = rgb_to_oklch(Rgb.from_hex(secondary_hex))
    light = _theme_palette(
        Theme.LIGHT,
        primary,
        light_accent or rgb_to_oklch(Rgb.from_hex(primary_scale[700])),
        primary_scale,
    )
    dark = _theme_palette(
        Theme.DARK,
        primary,
        dark_accent or rgb_to_oklch(Rgb.from_hex(primary_scale[300])),
        primary_scale,
    )
    original = Rgb.from_hex(primary_hex).to_hex()
    return BrandPalette(
        light=light,
        dark=dark,
        scale=primary_scale,
        primary_adjusted=light.primary != original or dark.primary != original,
        semantic_warnings=semantic_warnings(primary),
    )
