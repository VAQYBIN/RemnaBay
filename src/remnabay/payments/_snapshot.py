"""Условия тарифа на момент создания платежа (сквозное правило 2, 2.5, 2.6, 3.7, 3.37).

Цена и параметры доступа фиксируются в платеже, когда клиент нажал «Оплатить»:
если оператор изменит тариф, пока клиент платит, применяется то, что клиент видел.
"""

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, PositiveInt

from remnabay.domain.payments import Payment
from remnabay.domain.tariffs import Tariff, TariffType, TrafficResetStrategy
from remnabay.journal import JsonValue


class TariffSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    tariff_id: int
    name: str
    type: TariffType
    # У «только пакет» срока нет (v1)
    duration_days: PositiveInt | None
    price: Decimal
    device_limit: PositiveInt
    squad_uuids: list[UUID]
    traffic_limit_bytes: PositiveInt | None
    traffic_reset_strategy: TrafficResetStrategy | None

    @classmethod
    def of(cls, tariff: Tariff) -> TariffSnapshot:
        return cls(
            tariff_id=tariff.id,
            name=tariff.name,
            type=tariff.type,
            duration_days=tariff.duration_days,
            price=tariff.price,
            device_limit=tariff.device_limit,
            squad_uuids=list(tariff.squad_uuids),
            traffic_limit_bytes=tariff.traffic_limit_bytes,
            traffic_reset_strategy=tariff.traffic_reset_strategy,
        )

    @classmethod
    def of_payment(cls, payment: Payment) -> TariffSnapshot:
        return cls.model_validate(payment.tariff_snapshot)

    def stored(self) -> dict[str, JsonValue]:
        """Для колонки `tariff_snapshot` (JSON)."""
        dumped: dict[str, JsonValue] = self.model_dump(mode="json")
        return dumped
