"""Раздача собранной админки (0025): статические файлы Vite под `/admin/`.

Любой путь админки без файла отдаёт `index.html` — маршруты знает само
приложение. Файлы из `assets/` с хэшем в имени кэшируются надолго, `index.html` —
нет: после обновления магазина браузер сразу получит новую админку.
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse, RedirectResponse

ADMIN_URL = "/admin"
# В образе и при разработке пакет установлен из src/: сборка лежит рядом, в admin/dist
DEFAULT_DIST = Path(__file__).resolve().parents[3] / "admin" / "dist"
_IMMUTABLE = "public, max-age=31536000, immutable"
_NO_CACHE = "no-cache"


def build_router(dist: Path = DEFAULT_DIST) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    root = dist.resolve()

    @router.get(ADMIN_URL)
    async def admin_root() -> RedirectResponse:  # pyright: ignore[reportUnusedFunction]
        return RedirectResponse(f"{ADMIN_URL}/", status.HTTP_308_PERMANENT_REDIRECT)

    @router.get(ADMIN_URL + "/{path:path}")
    async def admin_file(path: str) -> FileResponse:  # pyright: ignore[reportUnusedFunction]
        index = root / "index.html"
        if not index.is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Админка не собрана")
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            cache = _IMMUTABLE if candidate.parent.name == "assets" else _NO_CACHE
            return FileResponse(candidate, headers={"Cache-Control": cache})
        return FileResponse(index, headers={"Cache-Control": _NO_CACHE})

    return router
