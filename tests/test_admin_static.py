"""Раздача собранной админки (0025, 0051): файлы под /admin/, остальное — index.html."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from remnabay.web import admin_static

INDEX = "<!doctype html><title>Админка</title>"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text(INDEX, encoding="utf-8")
    (tmp_path / "assets" / "app-1a2b.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return tmp_path


def _client(dist: Path) -> TestClient:
    app = FastAPI()
    app.include_router(admin_static.build_router(dist))
    return TestClient(app)


def test_admin_root_redirects_to_slash(dist: Path) -> None:
    response = _client(dist).get("/admin", follow_redirects=False)

    assert response.status_code == 308
    assert response.headers["location"] == "/admin/"


@pytest.mark.parametrize("path", ["/admin/", "/admin/login", "/admin/settings/brand"])
def test_app_routes_get_index(dist: Path, path: str) -> None:
    """Маршруты приложения отдают index.html без долгого кэша."""
    response = _client(dist).get(path)

    assert response.status_code == 200
    assert response.text == INDEX
    assert response.headers["cache-control"] == "no-cache"


def test_hashed_assets_are_cached_long(dist: Path) -> None:
    response = _client(dist).get("/admin/assets/app-1a2b.js")

    assert response.text == "console.log(1)"
    assert "immutable" in response.headers["cache-control"]


def test_other_files_are_served(dist: Path) -> None:
    assert _client(dist).get("/admin/favicon.svg").text == "<svg/>"


def test_files_outside_dist_are_not_served(dist: Path) -> None:
    (dist.parent / "secret.txt").write_text("секрет", encoding="utf-8")

    response = _client(dist).get("/admin/..%2Fsecret.txt")

    assert "секрет" not in response.text


def test_missing_build_is_404(tmp_path: Path) -> None:
    assert _client(tmp_path / "nothing").get("/admin/").status_code == 404
