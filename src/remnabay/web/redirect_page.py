"""Страница-переходник `/connect/<токен>` (блок 12, решения 0019, 0038, 0058).

- Автоматически открывает приложение и показывает кнопку на случай, если это не
  сработало, а ниже — ссылки на установку приложения из конфига (12.12).
- Язык — клиента, тема — устройства; если тема неизвестна — тёмная (12.13).
- Только бренд оператора: логотип, название, фирменный цвет; RemnaBay нет (12.15).
- Диплинк — только тот, что собрал сервер; адрес страницы его не содержит (12.10,
  12.11). Неизвестный или погасший токен — `redirect.invalid` (12.14).
"""

import html
import json
import secrets
from dataclasses import dataclass

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.brand import (
    BRAND_PRIMARY_COLOR,
    BRAND_SECONDARY_COLOR,
    AssetKind,
    asset_versions,
    brand_name,
    build_palette,
)
from remnabay.domain.clients import Client
from remnabay.guide import REDIRECT_PATH, Language, known_config_uuid, resolve, stored_config
from remnabay.messaging import load_texts
from remnabay.shop_settings import SHOP_LANGUAGE, get_setting
from remnabay.texts import Texts
from remnabay.web.admin._deps import DbSession
from remnabay.web.brand_files import asset_url

router = APIRouter(include_in_schema=False)


@dataclass(frozen=True)
class _Brand:
    name: str
    mark_url: str | None
    logo_url: str | None
    css: str


def _tokens(tokens: dict[str, str]) -> str:
    return "".join(f"--rb-{name}:{value};" for name, value in tokens.items())


async def _brand(session: AsyncSession) -> _Brand:
    palette = build_palette(
        await get_setting(session, BRAND_PRIMARY_COLOR),
        await get_setting(session, BRAND_SECONDARY_COLOR),
    )
    versions = await asset_versions(session)
    mark, logo = versions.get(AssetKind.MARK), versions.get(AssetKind.LOGO)
    # Тема устройства; неизвестна — тёмная (12.13, 0038)
    css = (
        f":root{{{_tokens(palette.dark.tokens)}}}"
        f"@media (prefers-color-scheme: light){{:root{{{_tokens(palette.light.tokens)}}}}}"
    )
    return _Brand(
        # Название не задано — пустое: RemnaBay на странице не упоминается (12.15)
        name=await brand_name(session),
        mark_url=asset_url(AssetKind.MARK, mark) if mark else None,
        logo_url=asset_url(AssetKind.LOGO, logo) if logo else None,
        css=css,
    )


_STYLE = """
*{box-sizing:border-box}
html{color-scheme:dark light}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
padding:24px 16px;font-family:"Golos Text Variable","Golos Text",system-ui,-apple-system,
"Segoe UI",Roboto,sans-serif;color:var(--rb-text);background-color:var(--rb-bg);
background-image:radial-gradient(60rem 40rem at 12% -10%,var(--rb-glow-1),transparent 70%),
radial-gradient(50rem 36rem at 100% 110%,var(--rb-glow-2),transparent 70%);
-webkit-font-smoothing:antialiased}
main{width:100%;max-width:420px;padding:28px 24px;border-radius:28px;
background:var(--rb-glass-module);border:1px solid var(--rb-glass-edge);
-webkit-backdrop-filter:blur(24px);backdrop-filter:blur(24px);text-align:center}
.brand{display:flex;align-items:center;justify-content:center;gap:10px;margin-bottom:24px;
font-weight:600;font-size:17px}
.brand img.mark{width:40px;height:40px;border-radius:12px}
.brand img.logo{max-height:40px;max-width:220px}
h1{margin:0 0 8px;font-size:22px;line-height:1.3;font-weight:600}
p{margin:0;color:var(--rb-text-muted);line-height:1.5}
.open{display:block;margin:24px 0 0;padding:14px 20px;border-radius:999px;font-weight:600;
text-decoration:none;background:var(--rb-primary);color:var(--rb-on-primary)}
.open:hover{background:var(--rb-primary-hover)}
.open:focus-visible,.install a:focus-visible{outline:2px solid var(--rb-focus);outline-offset:3px}
.install{margin-top:28px;padding-top:20px;border-top:1px solid var(--rb-border)}
.install h2{margin:0 0 12px;font-size:15px;font-weight:600}
.install ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px}
.install a{display:block;padding:10px 16px;border-radius:999px;text-decoration:none;
color:var(--rb-primary-text);background:var(--rb-primary-soft)}
"""


def _page(brand: _Brand, language: str, body: str, *, script: str = "") -> HTMLResponse:
    nonce = secrets.token_urlsafe(16)
    if brand.logo_url:
        logo, name = html.escape(brand.logo_url), html.escape(brand.name)
        head = f'<img class="logo" src="{logo}" alt="{name}">'
    else:
        mark = (
            f'<img class="mark" src="{html.escape(brand.mark_url)}" alt="">'
            if brand.mark_url
            else ""
        )
        head = f"{mark}<span>{html.escape(brand.name)}</span>"
    scripts = f'<script nonce="{nonce}">{script}</script>' if script else ""
    document = (
        f'<!doctype html><html lang="{html.escape(language)}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{html.escape(brand.name)}</title>"
        f'<style nonce="{nonce}">{brand.css}{_STYLE}</style></head>'
        f'<body><main><div class="brand">{head}</div>{body}</main>{scripts}</body></html>'
    )
    headers = {
        "Content-Security-Policy": (
            f"default-src 'none'; img-src 'self'; style-src 'nonce-{nonce}'; "
            f"script-src 'nonce-{nonce}'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'"
        ),
        # Ссылка на подписку — в теле страницы: её не должны сохранять ни кэш, ни Referer
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex",
    }
    return HTMLResponse(document, headers=headers)


def _script(link: str) -> str:
    """Открыть приложение сразу (12.12). JSON с экранированием «<» не закрывает тег."""
    target = json.dumps(link).replace("<", "\\u003c")
    return f"window.location.href={target};"


async def _invalid(session: AsyncSession, texts: Texts, language: str) -> HTMLResponse:
    body = f"<p>{html.escape(texts.render('redirect.invalid', language))}</p>"
    return _page(await _brand(session), language, body)


@router.get(REDIRECT_PATH + "/{token}", response_class=HTMLResponse)
async def redirect_page(token: str, session: DbSession) -> HTMLResponse:
    texts = await load_texts(session)
    default = await get_setting(session, SHOP_LANGUAGE)
    redirect = await resolve(session, token)
    if redirect is None:
        return await _invalid(session, texts, default)
    client = await session.get(Client, redirect.subscription.client_id)
    language = texts.language_for(client.language_code if client else None)
    config = await stored_config(session, known_config_uuid(redirect.subscription))
    if config is None:
        return await _invalid(session, texts, language)
    pick = Language(config, language, default).pick
    app = redirect.app.name
    escaped_link = html.escape(redirect.deeplink, quote=True)
    body = (
        f"<h1>{html.escape(texts.render('redirect.title', language, app_name=app))}</h1>"
        f"<p>{html.escape(texts.render('redirect.hint', language))}</p>"
        f'<a class="open" href="{escaped_link}">'
        f"{html.escape(texts.render('redirect.button', language, app_name=app))}</a>"
    )
    installs = redirect.app.install_links()
    if installs:
        items = "".join(
            f'<li><a href="{html.escape(button.link, quote=True)}" rel="noopener noreferrer">'
            f"{html.escape(pick(button.text) or app)}</a></li>"
            for button in installs
            if button.link.startswith(("https://", "http://"))
        )
        heading = html.escape(texts.render("redirect.not_installed", language))
        body += f'<section class="install"><h2>{heading}</h2><ul>{items}</ul></section>'
    return _page(await _brand(session), language, body, script=_script(redirect.deeplink))
