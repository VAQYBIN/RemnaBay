"""Версия магазина одна во всех местах выпуска (решения 0027, 0073)."""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as file:
        return tomllib.load(file)["project"]["version"]


@pytest.mark.parametrize("variant", ["panel", "caddy", "nginx"])
def test_0073_compose_image_version_matches_project(variant: str) -> None:
    """0073: compose каждого варианта установки ставит образ той же версии, что у проекта."""
    compose = (ROOT / "deploy" / variant / "compose.yaml").read_text(encoding="utf-8")

    images = re.findall(r"ghcr\.io/vaqybin/remnabay:\$\{REMNABAY_VERSION:-([^}]+)\}", compose)

    assert images == [_project_version()]


def test_0073_changelog_has_section_for_project_version() -> None:
    """0073: у текущей версии есть раздел в CHANGELOG.md, и он самый верхний."""
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    sections = re.findall(r"^## (\S+)", changelog, flags=re.MULTILINE)

    assert sections[0] == _project_version()
