"""Платежи: подтверждение оплаты, применение и разбор «оплачен, не применён» (блок 4).

Остальной код знает только этот интерфейс. Деньги пришли — услуга будет (0008):
платёж применяется операцией очереди с повторами; попытки исчерпаны — платёж ждёт
команду. Что именно записать в панель при покупке и продлении — шаг применения
(`PaymentApplier`), его даёт блок 3.
"""

from remnabay.payments._applier import (
    Applied,
    ApplierNotReadyError,
    NotReadyApplier,
    PaymentApplier,
)
from remnabay.payments._apply import (
    PROCESSING_NOTICE_DELAY,
    PaymentArgs,
    apply_payment,
    payment_subject,
    processing_notice,
)
from remnabay.payments._confirm import confirm_payment
from remnabay.payments._team import (
    PaymentActionError,
    PaymentCard,
    apply_as_new_subscription,
    bind_unknown_payment,
    payment_card,
    payments_needing_attention,
    resolve_payment_manually,
    retry_payment,
)

__all__ = [
    "PROCESSING_NOTICE_DELAY",
    "Applied",
    "ApplierNotReadyError",
    "NotReadyApplier",
    "PaymentActionError",
    "PaymentApplier",
    "PaymentArgs",
    "PaymentCard",
    "apply_as_new_subscription",
    "apply_payment",
    "bind_unknown_payment",
    "confirm_payment",
    "payment_card",
    "payment_subject",
    "payments_needing_attention",
    "processing_notice",
    "resolve_payment_manually",
    "retry_payment",
]
