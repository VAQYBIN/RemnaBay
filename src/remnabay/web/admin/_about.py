"""«О программе» (1.26, А12): RemnaBay, версия, лицензии. Доступен всем ролям."""

import os
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter
from pydantic import BaseModel

from remnabay.web.admin._deps import Member

REPOSITORY_URL = "https://github.com/VAQYBIN/RemnaBay"
LICENSE = "MIT"
FONT_LICENSE = "SIL Open Font License 1.1"

# Полный номер сборки образа, например 0.1.0-rc.4: его задаёт сборка образа по тегу
# версии. Версия пакета из pyproject.toml суффикса предварительной версии не знает
# (решение 0076)
IMAGE_VERSION_ENV = "REMNABAY_IMAGE_VERSION"

router = APIRouter(tags=["about"])


class AboutOut(BaseModel):
    version: str
    license: str
    repository_url: str
    font: str
    font_license: str


def shop_version() -> str:
    image_version = os.environ.get(IMAGE_VERSION_ENV, "").strip()
    if image_version:
        return image_version
    try:
        return version("remnabay")
    except PackageNotFoundError:
        return "dev"


@router.get("/about")
async def about(member: Member) -> AboutOut:
    del member
    return AboutOut(
        version=shop_version(),
        license=LICENSE,
        repository_url=REPOSITORY_URL,
        font="Golos Text",
        font_license=FONT_LICENSE,
    )
