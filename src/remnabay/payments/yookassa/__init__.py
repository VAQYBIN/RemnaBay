"""ЮКасса через контракт провайдера (3.31, 3.32).

Свой тонкий клиент на httpx2, как и клиент панели (0042): SDK ЮКассы синхронный.
Подтверждение оплаты проверяется запросом статуса платежа к API с ключами
оператора; тело уведомления — только подсказка, какой платёж проверить
(решение 0057, 3.28).
"""

from remnabay.payments.yookassa._client import (
    API_URL,
    YOOKASSA,
    YooKassaCredentials,
    YooKassaKind,
    YooKassaProvider,
    yookassa_http,
)

__all__ = [
    "API_URL",
    "YOOKASSA",
    "YooKassaCredentials",
    "YooKassaKind",
    "YooKassaProvider",
    "yookassa_http",
]
