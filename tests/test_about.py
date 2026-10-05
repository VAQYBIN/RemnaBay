"""«О программе» (1.26): доступен владельцу и помощнику."""

from collections.abc import AsyncGenerator
from importlib.metadata import version

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.domain.team import TeamRole
from tests.domain_support import add, make_team_member
from tests.web_support import Shop, running_shop


@pytest.fixture
async def shop(valid_env: dict[str, str], db_session: AsyncSession) -> AsyncGenerator[Shop]:
    del valid_env
    async with running_shop(db_session) as shop:
        yield shop


@pytest.mark.parametrize("role", list(TeamRole))
async def test_1_26_about_for_every_role(shop: Shop, role: TeamRole) -> None:
    """1.26: версия магазина, лицензия MIT, репозиторий, шрифт Golos Text и OFL."""
    member = make_team_member(telegram_id=700, role=role)
    await add(shop.session, member)
    await shop.sign_in(member)

    response = await shop.http.get("/api/admin/about")

    assert response.status_code == 200
    assert response.json() == {
        "version": version("remnabay"),
        "license": "MIT",
        "repository_url": "https://github.com/VAQYBIN/RemnaBay",
        "font": "Golos Text",
        "font_license": "SIL Open Font License 1.1",
    }


async def test_1_26_about_requires_login(shop: Shop) -> None:
    assert (await shop.http.get("/api/admin/about")).status_code == 401
