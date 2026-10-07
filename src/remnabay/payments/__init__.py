"""Платежи: счёт у провайдера, подтверждение оплаты, применение и разбор «оплачен, не
применён» (блоки 3 и 4).

Остальной код знает только этот интерфейс. С провайдером магазин работает только
через контракт (`PaymentProvider`, 3.27). Деньги пришли — услуга будет (0008):
платёж применяется операцией очереди с повторами; попытки исчерпаны — платёж ждёт
команду. Что именно записать в панель при покупке и продлении — шаг применения
(`PaymentApplier`).
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
from remnabay.payments._catalog import (
    CatalogError,
    Quote,
    can_purchase,
    can_renew,
    check_choice,
    disabled_in_panel,
    is_active,
    live_subscriptions,
    quote,
    renewal_tariff,
    tariffs_on_sale,
    trial_for_purchase,
)
from remnabay.payments._checkout import (
    INVOICE_LIFETIME,
    POLL_AFTER_EXPIRY,
    POLL_INTERVAL,
    Checkout,
    CheckoutError,
    InvoiceArgs,
    PaymentUnavailableError,
    PriceChangedError,
    cancel_by_client,
    decline,
    expire_invoice,
    open_invoice,
    poll_invoice,
    provider_reported,
    retry_callback,
)
from remnabay.payments._confirm import confirm_payment
from remnabay.payments._provider import (
    Invoice,
    InvoiceRequest,
    NotificationError,
    NotificationIgnoredError,
    PaymentProvider,
    ProviderAuthError,
    ProviderError,
    ProviderKind,
    ProviderPayment,
    ProviderStatus,
    ProviderUnavailableError,
)
from remnabay.payments._providers import (
    Connection,
    ConnectionState,
    ProviderKeysError,
    Providers,
)
from remnabay.payments._shop_applier import ApplyError, ShopApplier
from remnabay.payments._snapshot import TariffSnapshot
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
    "INVOICE_LIFETIME",
    "POLL_AFTER_EXPIRY",
    "POLL_INTERVAL",
    "PROCESSING_NOTICE_DELAY",
    "Applied",
    "ApplierNotReadyError",
    "ApplyError",
    "CatalogError",
    "Checkout",
    "CheckoutError",
    "Connection",
    "ConnectionState",
    "Invoice",
    "InvoiceArgs",
    "InvoiceRequest",
    "NotReadyApplier",
    "NotificationError",
    "NotificationIgnoredError",
    "PaymentActionError",
    "PaymentApplier",
    "PaymentArgs",
    "PaymentCard",
    "PaymentProvider",
    "PaymentUnavailableError",
    "PriceChangedError",
    "ProviderAuthError",
    "ProviderError",
    "ProviderKeysError",
    "ProviderKind",
    "ProviderPayment",
    "ProviderStatus",
    "ProviderUnavailableError",
    "Providers",
    "Quote",
    "ShopApplier",
    "TariffSnapshot",
    "apply_as_new_subscription",
    "apply_payment",
    "bind_unknown_payment",
    "can_purchase",
    "can_renew",
    "cancel_by_client",
    "check_choice",
    "confirm_payment",
    "decline",
    "disabled_in_panel",
    "expire_invoice",
    "is_active",
    "live_subscriptions",
    "open_invoice",
    "payment_card",
    "payment_subject",
    "payments_needing_attention",
    "poll_invoice",
    "processing_notice",
    "provider_reported",
    "quote",
    "renewal_tariff",
    "resolve_payment_manually",
    "retry_callback",
    "retry_payment",
    "tariffs_on_sale",
    "trial_for_purchase",
]
