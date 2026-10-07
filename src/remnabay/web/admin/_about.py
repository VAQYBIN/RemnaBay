"""«О программе» (1.26, А12): RemnaBay, версия, лицензии. Доступен всем ролям."""

from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter
from pydantic import BaseModel

from remnabay.web.admin._deps import Member

REPOSITORY_URL = "https://github.com/VAQYBIN/RemnaBay"
LICENSE = "MIT"
FONT_LICENSE = "SIL Open Font License 1.1"

router = APIRouter(tags=["about"])


class AboutOut(BaseModel):
    version: str
    license: str
    repository_url: str
    font: str
    font_license: str


def shop_version() -> str:
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
