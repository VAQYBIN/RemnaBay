"""Логотипы оператора по публичному адресу `/brand/<вид>?v=<хэш>` (0051).

Нужны экрану входа (до входа) и странице-переходнику (блок 12). Адрес с хэшем
содержимого кэшируется браузером надолго: новый логотип — новый адрес.
SVG отдаётся с запретом скриптов: файл загружал человек, а не рама.
"""

from fastapi import APIRouter, HTTPException, Response, status

from remnabay.brand import SVG, AssetKind, get_asset
from remnabay.web.admin._deps import DbSession

BRAND_FILES_PREFIX = "/brand"
_IMMUTABLE = "public, max-age=31536000, immutable"
_SVG_POLICY = "default-src 'none'; style-src 'unsafe-inline'; sandbox"

router = APIRouter(prefix=BRAND_FILES_PREFIX, include_in_schema=False)


def asset_url(kind: AssetKind, digest: str) -> str:
    return f"{BRAND_FILES_PREFIX}/{kind.value}?v={digest[:16]}"


@router.get("/{kind}")
async def brand_file(kind: AssetKind, session: DbSession, v: str | None = None) -> Response:
    asset = await get_asset(session, kind)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    headers = {
        "Cache-Control": _IMMUTABLE if v and asset.sha256.startswith(v) else "no-cache",
        "X-Content-Type-Options": "nosniff",
    }
    if asset.content_type == SVG:
        headers["Content-Security-Policy"] = _SVG_POLICY
    return Response(asset.data, media_type=asset.content_type, headers=headers)
