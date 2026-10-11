"""Страница-переходник: непрозрачный токен вместо адреса перенаправления (12.10–12.14).

- Кнопка импорта в приложение ведёт на страницу магазина с токеном. Диплинк
  собирается на сервере из конфига и ссылки подписки; произвольный адрес страница
  не принимает (12.10), ссылка на подписку в адрес страницы не попадает (12.11).
- Токен бессрочный, но привязан к ссылке подписки, для которой выдан: после отзыва
  ссылки (4.29) старые кнопки гаснут — вместе со ссылкой могло утечь и сообщение с
  инструкцией (12.14, решение 0058).
- Одна и та же кнопка получает один и тот же токен: инструкцию открывают сколько
  угодно раз, а таблица не растёт.
"""

import secrets
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, String, Text, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from remnabay.db import Base
from remnabay.domain._types import CreatedAt
from remnabay.domain.subscriptions import Subscription
from remnabay.guide._config import ConfigApp
from remnabay.guide._store import known_config_uuid, stored_config

REDIRECT_PATH = "/connect"
_TOKEN_BYTES = 24
# Переменные шаблона диплинка в конфиге Subscription Page
_LINK_VARIABLE = "{{SUBSCRIPTION_LINK}}"
_USERNAME_VARIABLE = "{{USERNAME}}"
# Схемы, которые нельзя открывать со страницы магазина
_FORBIDDEN_SCHEMES = ("javascript:", "data:", "vbscript:", "file:")


class RedirectLink(Base):
    """Кнопка импорта в приложение: подписка, ссылка, для которой выдана, приложение."""

    __tablename__ = "redirect_links"
    __table_args__ = (
        UniqueConstraint("subscription_id", "short_uuid", "platform", "app_name", "template"),
    )

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subscriptions.id"), index=True
    )
    # Ссылка подписки, для которой выдан токен: после отзыва ссылки токен гаснет (12.14)
    short_uuid: Mapped[str] = mapped_column(String(64))
    platform: Mapped[str] = mapped_column(String(64))
    app_name: Mapped[str] = mapped_column(String(256))
    # Шаблон диплинка из конфига: «happ://add/{{SUBSCRIPTION_LINK}}»
    template: Mapped[str] = mapped_column(Text)
    created_at: Mapped[CreatedAt]


async def redirect_token(
    session: AsyncSession, subscription: Subscription, platform: str, app_name: str, template: str
) -> str:
    """Токен кнопки импорта: уже выданный для той же ссылки и кнопки или новый."""
    values = {
        "subscription_id": subscription.id,
        "short_uuid": subscription.panel_short_uuid,
        "platform": platform,
        "app_name": app_name,
        "template": template,
    }
    created = await session.scalar(
        insert(RedirectLink)
        .values(token=secrets.token_urlsafe(_TOKEN_BYTES), **values)
        .on_conflict_do_nothing()
        .returning(RedirectLink.token)
    )
    if created is not None:
        return created
    existing = await session.scalar(
        select(RedirectLink.token).where(
            *(getattr(RedirectLink, name) == value for name, value in values.items())
        )
    )
    if existing is None:
        raise RuntimeError("Токен страницы-переходника не сохранён")
    return existing


def deeplink(template: str, subscription: Subscription) -> str:
    """Диплинк из шаблона конфига: подставляются ссылка подписки и имя пользователя."""
    return template.replace(_LINK_VARIABLE, subscription.subscription_url).replace(
        _USERNAME_VARIABLE, subscription.panel_username
    )


def _safe(link: str) -> bool:
    return bool(link) and not link.strip().lower().startswith(_FORBIDDEN_SCHEMES)


@dataclass(frozen=True)
class Redirect:
    """Что показать на странице-переходнике."""

    subscription: Subscription
    app: ConfigApp
    deeplink: str
    created_at: datetime


async def resolve(session: AsyncSession, token: str) -> Redirect | None:
    """Куда ведёт токен. `None` — `redirect.invalid`: токен неизвестен, подписка
    удалена, ссылку подписки обновили или приложения больше нет в конфиге (12.14)."""
    link = await session.get(RedirectLink, token)
    if link is None:
        return None
    subscription = await session.get(Subscription, link.subscription_id)
    if subscription is None or subscription.deleted_at is not None:
        return None
    if subscription.panel_short_uuid != link.short_uuid:
        return None
    config = await stored_config(session, known_config_uuid(subscription))
    platform = config.platforms.get(link.platform) if config is not None else None
    app = platform.app(link.app_name) if platform is not None else None
    if app is None:
        return None
    templates = [button.link for button in app.import_buttons()]
    if not templates:
        return None
    # Шаблон кнопки мог поменяться в конфиге — тогда первая кнопка импорта приложения
    template = link.template if link.template in templates else templates[0]
    target = deeplink(template, subscription)
    if not _safe(target):
        return None
    return Redirect(subscription=subscription, app=app, deeplink=target, created_at=link.created_at)
