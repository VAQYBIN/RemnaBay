"""Цвет: sRGB ↔ OKLCH, контраст WCAG 2.2, подстройка светлоты с сохранением тона.

OKLCH — пространство, где светлота (L), насыщенность (C) и тон (h) меняются
независимо и равномерно для глаза (Björn Ottosson, «A perceptual color space for
image processing», 2020). Поэтому рама меняет L и C цвета оператора, а тон h
остаётся тем, что выбрал оператор (решение 0038).
"""

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")
# Шаг поиска светлоты: 0,005 — меньше заметной глазу разницы
_LIGHTNESS_STEP = 0.005
_GAMUT_EPSILON = 1e-6


@dataclass(frozen=True)
class Rgb:
    """Цвет sRGB, каналы 0…1 в гамма-кодировке (как в CSS)."""

    r: float
    g: float
    b: float

    @classmethod
    def from_hex(cls, value: str) -> Rgb:
        match = _HEX.match(value.strip())
        if match is None:
            raise ValueError(f"Цвет в формате #RRGGBB, а не {value!r}")
        digits = match.group(1)
        r, g, b = (int(digits[i : i + 2], 16) / 255 for i in (0, 2, 4))
        return cls(r, g, b)

    def quantized(self) -> Rgb:
        """Цвет, каким он окажется в CSS: по 8 бит на канал."""
        return Rgb.from_hex(self.to_hex())

    def to_hex(self) -> str:
        return "#" + "".join(f"{round(_clamp(c) * 255):02x}" for c in (self.r, self.g, self.b))

    def css(self, alpha: float = 1.0) -> str:
        if alpha >= 1:
            return self.to_hex()
        r, g, b = (round(_clamp(c) * 255) for c in (self.r, self.g, self.b))
        return f"rgb({r} {g} {b} / {alpha:g})"


@dataclass(frozen=True)
class Oklch:
    l: float  # noqa: E741 — общепринятое имя светлоты в OKLCH
    c: float
    h: float

    def with_lightness(self, lightness: float) -> Oklch:
        """Та же насыщенность и тон, другая светлота; насыщенность урезается до
        возможной в sRGB при этой светлоте."""
        lightness = _clamp(lightness)
        return Oklch(lightness, min(self.c, max_chroma(lightness, self.h)), self.h)

    def to_rgb(self) -> Rgb:
        """Цвет sRGB, округлённый до 8 бит: контраст проверяется у того, что увидит
        браузер."""
        return oklch_to_rgb(self).quantized()


def _clamp(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def _to_linear(channel: float) -> float:
    if channel <= 0.04045:
        return channel / 12.92
    return ((channel + 0.055) / 1.055) ** 2.4


def _from_linear(channel: float) -> float:
    if channel <= 0.0031308:
        return 12.92 * channel
    return 1.055 * channel ** (1 / 2.4) - 0.055


def _linear_from_oklch(color: Oklch) -> tuple[float, float, float]:
    a = color.c * math.cos(math.radians(color.h))
    b = color.c * math.sin(math.radians(color.h))
    # Отклики длинных, средних и коротких колбочек (LMS)
    long = (color.l + 0.3963377774 * a + 0.2158037573 * b) ** 3
    medium = (color.l - 0.1055613458 * a - 0.0638541728 * b) ** 3
    short = (color.l - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (
        4.0767416621 * long - 3.3077115913 * medium + 0.2309699292 * short,
        -1.2684380046 * long + 2.6097574011 * medium - 0.3413193965 * short,
        -0.0041960863 * long - 0.7034186147 * medium + 1.7076147010 * short,
    )


def oklch_to_rgb(color: Oklch) -> Rgb:
    r, g, b = (_from_linear(_clamp(channel)) for channel in _linear_from_oklch(color))
    return Rgb(r, g, b)


def rgb_to_oklch(color: Rgb) -> Oklch:
    r, g, b = (_to_linear(channel) for channel in (color.r, color.g, color.b))
    long = math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
    medium = math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
    short = math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
    lightness = 0.2104542553 * long + 0.7936177850 * medium - 0.0040720468 * short
    a = 1.9779984951 * long - 2.4285922050 * medium + 0.4505937099 * short
    b_ = 0.0259040371 * long + 0.7827717662 * medium - 0.8086757660 * short
    chroma = math.hypot(a, b_)
    hue = math.degrees(math.atan2(b_, a)) % 360 if chroma > 1e-4 else 0.0
    return Oklch(lightness, chroma, hue)


def in_gamut(color: Oklch) -> bool:
    return all(
        -_GAMUT_EPSILON <= channel <= 1 + _GAMUT_EPSILON for channel in _linear_from_oklch(color)
    )


def max_chroma(lightness: float, hue: float) -> float:
    """Наибольшая насыщенность цвета с этим тоном и светлотой, которую показывает sRGB."""
    low, high = 0.0, 0.4
    for _ in range(30):
        middle = (low + high) / 2
        if in_gamut(Oklch(lightness, middle, hue)):
            low = middle
        else:
            high = middle
    return low


def relative_luminance(color: Rgb) -> float:
    """Относительная яркость по WCAG 2.2."""
    r, g, b = (_to_linear(channel) for channel in (color.r, color.g, color.b))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(first: Rgb, second: Rgb) -> float:
    """Контраст по WCAG 2.2: от 1 до 21."""
    lighter, darker = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def composite(top: Rgb, alpha: float, under: Rgb) -> Rgb:
    """Полупрозрачный цвет поверх непрозрачного — так, как смешивает браузер."""
    return Rgb(
        top.r * alpha + under.r * (1 - alpha),
        top.g * alpha + under.g * (1 - alpha),
        top.b * alpha + under.b * (1 - alpha),
    )


def nearest_lightness(start: Oklch, passes: Callable[[Rgb], bool]) -> Oklch | None:
    """Ближайший по светлоте цвет того же тона, который проходит проверку.

    Насыщенность сохраняется, пока её показывает sRGB. `None` — если не подошла
    ни одна светлота.
    """
    steps = math.ceil(1 / _LIGHTNESS_STEP)
    for step in range(steps + 1):
        for sign in (1, -1) if step else (1,):
            lightness = start.l + sign * step * _LIGHTNESS_STEP
            if not 0 <= lightness <= 1:
                continue
            candidate = start.with_lightness(lightness)
            if passes(candidate.to_rgb()):
                return candidate
    return None


def min_contrast(color: Rgb, backgrounds: Sequence[Rgb]) -> float:
    return min(contrast(color, background) for background in backgrounds)


def hue_distance(first: float, second: float) -> float:
    difference = abs(first - second) % 360
    return min(difference, 360 - difference)
