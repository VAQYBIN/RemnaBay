"""Настройки «Вход в админку» (04-operator-settings.md)."""

from datetime import timedelta
from typing import Annotated

from pydantic import Field, TypeAdapter

from remnabay.shop_settings import ShopSetting

type _LoginTtl = Annotated[timedelta, Field(ge=timedelta(minutes=1), le=timedelta(hours=1))]
type _SessionTtl = Annotated[timedelta, Field(ge=timedelta(hours=1), le=timedelta(days=365))]

# Срок действия подтверждения входа (1.5)
LOGIN_TTL = ShopSetting("admin.login_ttl", TypeAdapter[timedelta](_LoginTtl), timedelta(minutes=5))
# Срок сессии; отзыв доступа завершает её сразу
SESSION_TTL = ShopSetting(
    "admin.session_ttl", TypeAdapter[timedelta](_SessionTtl), timedelta(days=30)
)
