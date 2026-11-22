"""Шкала и контраст цвета оператора (1.24, решение 0038).

Проверка независима от кода подбора: токены разбираются как CSS, поверхности
собираются заново — стекло каждого уровня поверх фона и поверх цветных пятен.
"""

import re

import pytest

from remnabay.brand._color import Rgb, composite, contrast, hue_distance, rgb_to_oklch
from remnabay.brand._theme import (
    REMNABAY_PRIMARY,
    SCALE_STEPS,
    Semantic,
    Theme,
    build_palette,
)

_RGBA = re.compile(r"^rgb\((\d+) (\d+) (\d+) / ([\d.]+)\)$")

# «Трудные» цвета оператора (DESIGN.md, Do's): очень светлый, очень тёмный, близкие
# к смысловым, чистые цвета экрана, серый
HARD_COLORS = [
    REMNABAY_PRIMARY,
    "#F5F5F5",
    "#FFFFFF",
    "#0A0A14",
    "#000000",
    "#FFFF00",
    "#0000FF",
    "#22C55E",
    "#E53935",
    "#F59E0B",
    "#808080",
    "#7C3AED",
]
TEXT_TOKENS = ("text", "text-muted", "primary-text", "accent-text")
SEMANTIC_NAMES = [s.value for s in Semantic]


def _parse(value: str) -> tuple[Rgb, float]:
    match = _RGBA.match(value)
    if match:
        r, g, b = (int(match.group(i)) / 255 for i in (1, 2, 3))
        return Rgb(r, g, b), float(match.group(4))
    return Rgb.from_hex(value), 1.0


def _surfaces(tokens: dict[str, str]) -> list[Rgb]:
    background, _ = _parse(tokens["bg"])
    unders = [background, _parse(tokens["glow-1"])[0], _parse(tokens["glow-2"])[0]]
    result: list[Rgb] = []
    for layer in ("glass-base", "glass-module", "glass-float"):
        glass, alpha = _parse(tokens[layer])
        result.extend(composite(glass, alpha, under) for under in unders)
    return result


@pytest.mark.parametrize("theme", list(Theme))
@pytest.mark.parametrize("color", HARD_COLORS)
def test_1_24_text_contrast_on_every_surface(color: str, theme: Theme) -> None:
    """1.24: текст и ссылки — не ниже 4,5:1 на любой поверхности рамы."""
    tokens = build_palette(color, None).theme(theme).tokens
    surfaces = _surfaces(tokens)

    for name in TEXT_TOKENS:
        text, _ = _parse(tokens[name])
        worst = min(contrast(text, surface) for surface in surfaces)
        assert worst >= 4.5, f"{name}: {worst:.2f}"


@pytest.mark.parametrize("theme", list(Theme))
@pytest.mark.parametrize("color", HARD_COLORS)
def test_1_24_primary_button_contrast(color: str, theme: Theme) -> None:
    """1.24: главная кнопка — 3:1 к поверхностям, текст на ней — 4,5:1."""
    tokens = build_palette(color, None).theme(theme).tokens
    fill, _ = _parse(tokens["primary"])
    on, _ = _parse(tokens["on-primary"])

    assert min(contrast(fill, s) for s in _surfaces(tokens)) >= 3.0
    assert contrast(fill, on) >= 4.5


@pytest.mark.parametrize("theme", list(Theme))
@pytest.mark.parametrize("color", HARD_COLORS)
def test_1_24_hue_is_kept(color: str, theme: Theme) -> None:
    """1.24: подстраиваются светлота и насыщенность, тон остаётся прежним."""
    original = rgb_to_oklch(Rgb.from_hex(color))
    if original.c < 0.03:
        pytest.skip("у серого цвета тона нет")
    tokens = build_palette(color, None).theme(theme).tokens

    for name in ("primary", "primary-text"):
        adjusted = rgb_to_oklch(_parse(tokens[name])[0])
        if adjusted.c >= 0.03:
            assert hue_distance(adjusted.h, original.h) < 4, name


@pytest.mark.parametrize("theme", list(Theme))
def test_1_24_semantic_colors_do_not_depend_on_brand(theme: Theme) -> None:
    """1.24: смысловые цвета (успех, внимание, ошибка) от бренда не меняются."""
    palettes = [build_palette(color, None).theme(theme).tokens for color in HARD_COLORS]

    for name in SEMANTIC_NAMES:
        for suffix in ("", "-text"):
            key = f"{name}{suffix}"
            assert len({p[key] for p in palettes}) == 1, key


@pytest.mark.parametrize("theme", list(Theme))
@pytest.mark.parametrize("color", HARD_COLORS)
def test_1_24_semantic_colors_readable_with_any_brand(color: str, theme: Theme) -> None:
    tokens = build_palette(color, None).theme(theme).tokens
    surfaces = _surfaces(tokens)

    for name in SEMANTIC_NAMES:
        text, _ = _parse(tokens[f"{name}-text"])
        fill, _ = _parse(tokens[name])
        on, _ = _parse(tokens[f"on-{name}"])
        assert min(contrast(text, s) for s in surfaces) >= 4.5, name
        assert min(contrast(fill, s) for s in surfaces) >= 3.0, name
        assert contrast(fill, on) >= 4.5, name


@pytest.mark.parametrize(
    ("color", "warnings"),
    [
        ("#22C55E", (Semantic.SUCCESS,)),
        ("#E53935", (Semantic.DANGER,)),
        ("#F59E0B", (Semantic.WARNING,)),
        (REMNABAY_PRIMARY, ()),
        ("#0000FF", ()),
        ("#808080", ()),
    ],
)
def test_1_24_warning_when_close_to_semantic(color: str, warnings: tuple[Semantic, ...]) -> None:
    """1.24: основной цвет, близкий по тону к смысловому, — предупреждение."""
    assert build_palette(color, None).semantic_warnings == warnings


def test_1_24_adjusted_color_is_reported() -> None:
    """1.24: в настройках видно, каким цвет станет, если его пришлось подстроить."""
    palette = build_palette("#F5F5F5", None)

    assert palette.primary_adjusted is True
    assert palette.light.primary != "#f5f5f5"


def test_1_24_scale_has_eleven_steps_of_one_hue() -> None:
    """1.24: из основного цвета строится шкала оттенков одного тона."""
    palette = build_palette("#7C3AED", None)
    hue = rgb_to_oklch(Rgb.from_hex("#7C3AED")).h

    assert list(palette.scale) == list(SCALE_STEPS)
    lightness = [rgb_to_oklch(Rgb.from_hex(v)).l for v in palette.scale.values()]
    assert lightness == sorted(lightness, reverse=True)
    for value in palette.scale.values():
        assert hue_distance(rgb_to_oklch(Rgb.from_hex(value)).h, hue) < 4


def test_1_24_secondary_color_is_never_the_primary_button() -> None:
    """1.24: дополнительный цвет не используется для кнопки целевого действия."""
    with_secondary = build_palette(REMNABAY_PRIMARY, "#E9B872")
    without = build_palette(REMNABAY_PRIMARY, None)

    for theme in Theme:
        assert with_secondary.theme(theme).primary == without.theme(theme).primary
        tokens = with_secondary.theme(theme).tokens
        assert tokens["primary"] == without.theme(theme).tokens["primary"]
        accent = rgb_to_oklch(Rgb.from_hex(tokens["accent"]))
        assert hue_distance(accent.h, rgb_to_oklch(Rgb.from_hex("#E9B872")).h) < 4


def test_0038_accent_from_primary_scale_without_secondary() -> None:
    """0038: без дополнительного цвета акценты — из шкалы основного, не песок RemnaBay."""
    tokens = build_palette("#7C3AED", None).light.tokens
    accent = rgb_to_oklch(Rgb.from_hex(tokens["accent"]))

    assert hue_distance(accent.h, rgb_to_oklch(Rgb.from_hex("#7C3AED")).h) < 4


def test_invalid_color_is_rejected() -> None:
    with pytest.raises(ValueError, match="#RRGGBB"):
        build_palette("teal", None)
