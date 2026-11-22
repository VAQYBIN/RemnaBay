"""Токены бренда RemnaBay по умолчанию как CSS — стартовые значения админки (1.23).

Админка показывает их до ответа `/api/admin/brand`, чтобы экран не мигал чужим
цветом. Файл генерируется (`remnabay default-brand-css`); CI проверяет, что он
совпадает с рамой.
"""

from remnabay.brand._theme import REMNABAY_PRIMARY, build_palette

_HEADER = """/* Сгенерировано: remnabay default-brand-css (scripts/admin-codegen.sh).
 * Бренд RemnaBay по умолчанию — до ответа /api/admin/brand. Не править вручную. */
"""


def _block(selector: str, tokens: dict[str, str]) -> str:
    lines = "".join(f"  --rb-{name}: {value};\n" for name, value in tokens.items())
    return f"{selector} {{\n{lines}}}\n"


def default_brand_css() -> str:
    palette = build_palette(REMNABAY_PRIMARY, None)
    return (
        _HEADER
        + _block(':root,\n:root[data-theme="dark"]', palette.dark.tokens)
        + "\n"
        + _block(':root[data-theme="light"]', palette.light.tokens)
    )
