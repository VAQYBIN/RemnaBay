"""Применение покупки и продления: что записать в панель и магазин (блок 3).

Правила:
- Покупка создаёт пользователя в панели с параметрами тарифа (3.6). Если у клиента
  единственная подписка — триальная, покупка применяется к ней (3.16). Если за ту
  же операцию уже применён другой счёт, оплата продлевает созданную им подписку,
  а команда получает уведомление (3.11).
- Продление: активная подписка — от даты окончания, истёкшая — от момента
  подтверждения оплаты (3.17, 3.18, 3.22). Пользователь панели и ссылка не меняются
  (3.19).
- Любое применение тарифа записывает в панель все параметры доступа тарифа в том
  виде, в каком они зафиксированы в платеже (3.37).
- Перед изменением панели магазин сверяет её состояние: не создан ли уже
  пользователь, не продлена ли уже подписка прошлой попыткой (4.15). Отрезки
  срока меняются только после того, как панель приняла изменение (3.25, 4.34).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay import runtime
from remnabay.domain.payments import Payment, PaymentPurpose, PaymentState
from remnabay.domain.subscriptions import SegmentKind, Subscription, TermSegment
from remnabay.domain.tariffs import TariffType
from remnabay.journal import Actor, Outcome, Subject, record
from remnabay.messaging import notify_team
from remnabay.panel import TrafficStrategy, UpdateUser, User, UserStatus
from remnabay.panel_sync import accept_access, accept_user, term_end
from remnabay.panel_users import Access, NewUserError, new_subscription, panel_user
from remnabay.payments._applier import Applied
from remnabay.payments._apply import money
from remnabay.payments._catalog import live_subscriptions, trial_for_purchase
from remnabay.payments._snapshot import TariffSnapshot

SUBSCRIPTION_SUBJECT = "subscription"
# Панель хранит дату с точностью до миллисекунд: даты ближе секунды — одна и та же
_SAME_MOMENT = timedelta(seconds=1)


class ApplyError(Exception):
    """Применить нельзя: например, пользователя подписки удалили в панели. Операция
    повторяется, затем платёж ждёт команду (сценарий продления, 4.20)."""


def _same_moment(first: datetime, second: datetime) -> bool:
    return abs(first - second) < _SAME_MOMENT


@dataclass(frozen=True)
class _Terms:
    """Чей платёж, когда подтверждена оплата и сколько дней она даёт."""

    client_id: int
    paid_at: datetime
    days: timedelta


class ShopApplier:
    """Шаг применения платежа магазина (`PaymentApplier`)."""

    def __init__(self, *, dev_mode: bool) -> None:
        # В режиме разработки — свой префикс имён в панели (решение 0057)
        self._dev_mode = dev_mode

    async def apply(self, session: AsyncSession, payment: Payment) -> Applied:
        snapshot = TariffSnapshot.of_payment(payment)
        if snapshot.type != TariffType.TERM_UNLIMITED or snapshot.duration_days is None:
            # В MVP продаётся только «срок + безлимит» (2.3); остальные типы — v1
            raise ApplyError(f"Тип тарифа {snapshot.type} применяется с v1")
        if payment.client_id is None or payment.paid_at is None:
            raise ApplyError("Платёж не привязан к клиенту или не оплачен")
        terms = _Terms(payment.client_id, payment.paid_at, timedelta(days=snapshot.duration_days))
        if payment.purpose == PaymentPurpose.RENEWAL:
            subscription = await self._renewed(session, payment)
            applied = await self._extend(session, payment, subscription, snapshot, terms)
            await self._tell_team_about_double_payment(session, payment, subscription)
            return applied
        if payment.purpose != PaymentPurpose.PURCHASE:
            raise ApplyError("Смена тарифа применяется с v1")
        if payment.new_subscription:
            return await self._create(session, payment, snapshot, terms)
        sibling = await self._applied_sibling(session, payment)
        if sibling is not None:
            applied = await self._extend(session, payment, sibling, snapshot, terms)
            await self._tell_team_about_double_payment(session, payment, sibling)
            return applied
        trial = trial_for_purchase(await live_subscriptions(session, terms.client_id))
        if trial is not None:
            return await self._extend(session, payment, trial, snapshot, terms, from_trial=True)
        return await self._create(session, payment, snapshot, terms)

    async def _renewed(self, session: AsyncSession, payment: Payment) -> Subscription:
        subscription = (
            await session.get(Subscription, payment.subscription_id)
            if payment.subscription_id is not None
            else None
        )
        if subscription is None or subscription.client_id != payment.client_id:
            raise ApplyError("Подписка для продления не найдена")
        if subscription.deleted_at is not None:
            raise ApplyError("Пользователя подписки удалили в панели — применить некуда")
        return subscription

    async def _applied_sibling(
        self, session: AsyncSession, payment: Payment
    ) -> Subscription | None:
        """Подписка, которую создал другой оплаченный счёт той же операции (3.11)."""
        if payment.operation_id is None:
            return None
        subscription_id = await session.scalar(
            select(Payment.subscription_id)
            .where(
                Payment.operation_id == payment.operation_id,
                Payment.id != payment.id,
                Payment.applied_at.is_not(None),
                Payment.subscription_id.is_not(None),
            )
            .order_by(Payment.applied_at)
            .limit(1)
        )
        if subscription_id is None:
            return None
        subscription = await session.get(Subscription, subscription_id)
        if subscription is None or subscription.deleted_at is not None:
            return None
        return subscription

    async def _tell_team_about_double_payment(
        self, session: AsyncSession, payment: Payment, subscription: Subscription
    ) -> None:
        """Оплачены оба счёта одной операции — команда получает уведомление (3.11)."""
        if payment.operation_id is None:
            return
        other_paid = await session.scalar(
            select(Payment.id)
            .where(
                Payment.operation_id == payment.operation_id,
                Payment.id != payment.id,
                Payment.applied_at.is_not(None),
                Payment.state != PaymentState.RESOLVED_MANUALLY,
            )
            .limit(1)
        )
        if other_paid is None:
            return
        await notify_team(
            session,
            "team.double_payment",
            variables={
                "amount": await money(session, payment),
                "subscription_name": subscription.name,
                # Подменяется датой из `dates`; пусто — если срока нет
                "end_date": "",
            },
            dates=_end_date(subscription),
        )

    async def _extend(
        self,
        session: AsyncSession,
        payment: Payment,
        subscription: Subscription,
        snapshot: TariffSnapshot,
        terms: _Terms,
        *,
        from_trial: bool = False,
    ) -> Applied:
        """Продление подписки или покупка поверх триала: пользователь и ссылка прежние.

        Строка подписки блокируется до обращения к панели: сверка этой подписки и
        другое её продление ждут, пока применение не запишет отрезок, — иначе сверка
        приняла бы продлённый срок за изменение в панели (4.17)."""
        await session.refresh(subscription, with_for_update=True)
        panel = runtime.current().panel
        user = await panel.get_user(subscription.panel_user_id)
        if user is None:
            raise ApplyError("Пользователя подписки удалили в панели — применить некуда")
        paid_at, days = terms.paid_at, terms.days
        known_end = await term_end(session, subscription)
        expected_end = max(known_end or paid_at, paid_at) + days
        if _same_moment(user.expire_at, expected_end):
            # Прошлая попытка уже продлила подписку в панели, а магазин об этом не
            # записал (оборвалась): второй раз не продлеваем (4.15)
            start = expected_end - days
        else:
            await accept_user(session, subscription, user)
            start = max(user.expire_at, paid_at)
            user = await panel.update_user(self._access(user, snapshot, start + days))
        if from_trial:
            # Новый оплаченный период начинается с чистого листа (3.16)
            user = await panel.reset_traffic(user.id)
        accept_access(subscription, user)
        await self._add_segment(session, payment, subscription, start, user.expire_at)
        subscription.tariff_id = snapshot.tariff_id
        subscription.is_trial = False
        await self._journal(
            session,
            subscription,
            "subscription.purchased" if from_trial else "subscription.renewed",
            payment,
        )
        if user.status == UserStatus.DISABLED:
            # Подписку отключил оператор: срок продлён, пользователь остаётся
            # отключённым, решает команда (4.7)
            await notify_team(
                session,
                "team.paid_disabled_subscription",
                variables={
                    "amount": await money(session, payment),
                    "subscription_name": subscription.name,
                    "end_date": "",
                },
                dates=_end_date(subscription),
            )
        return Applied(subscription_id=subscription.id, created=False, purchase=from_trial)

    def _access(self, user: User, snapshot: TariffSnapshot, expire_at: datetime) -> UpdateUser:
        """Все параметры доступа тарифа (3.37). Безлимит — лимит трафика 0; стратегия
        сброса записывается, только если отличается, чтобы не сбить расписание."""
        if user.traffic_limit_strategy == TrafficStrategy.NO_RESET:
            return UpdateUser(
                id=user.id,
                expire_at=expire_at,
                hwid_device_limit=snapshot.device_limit,
                active_internal_squads=snapshot.squad_uuids,
                traffic_limit_bytes=0,
            )
        return UpdateUser(
            id=user.id,
            expire_at=expire_at,
            hwid_device_limit=snapshot.device_limit,
            active_internal_squads=snapshot.squad_uuids,
            traffic_limit_bytes=0,
            traffic_limit_strategy=TrafficStrategy.NO_RESET,
        )

    async def _create(
        self, session: AsyncSession, payment: Payment, snapshot: TariffSnapshot, terms: _Terms
    ) -> Applied:
        """Новая подписка: пользователь панели с параметрами тарифа (3.6, 3.14)."""
        days = terms.days
        # Срок новой подписки — от момента её создания в панели: до этого доступа у
        # клиента не было, задержка (например, панель недоступна) срок не съедает (0057)
        access = Access(
            expire_at=datetime.now(UTC) + days,
            traffic_limit_bytes=0,
            device_limit=snapshot.device_limit,
            squad_uuids=snapshot.squad_uuids,
        )
        try:
            user = await panel_user(session, terms.client_id, access, dev_mode=self._dev_mode)
        except NewUserError as error:
            raise ApplyError(str(error)) from error
        subscription = await new_subscription(
            session, terms.client_id, user, tariff_id=snapshot.tariff_id
        )
        await self._add_segment(
            session, payment, subscription, user.expire_at - days, user.expire_at
        )
        await self._journal(session, subscription, "subscription.created", payment)
        return Applied(subscription_id=subscription.id, created=True, purchase=True)

    async def _add_segment(
        self,
        session: AsyncSession,
        payment: Payment,
        subscription: Subscription,
        start: datetime,
        end: datetime,
    ) -> None:
        """Отрезок срока со стоимостью, равной фактически оплаченной сумме (3.25)."""
        session.add(
            TermSegment(
                subscription_id=subscription.id,
                kind=SegmentKind.PAYMENT,
                payment_id=payment.id,
                starts_at=start,
                ends_at=end,
                cost=payment.amount,
            )
        )
        await session.flush()

    async def _journal(
        self, session: AsyncSession, subscription: Subscription, action: str, payment: Payment
    ) -> None:
        """Изменение подписки — в журнал (4.25)."""
        await record(
            session,
            actor=Actor.SYSTEM,
            action=action,
            outcome=Outcome.SUCCESS,
            subject=Subject(SUBSCRIPTION_SUBJECT, subscription.id),
            details={
                "payment_id": payment.id,
                "tariff_id": subscription.tariff_id,
                "expires_at": subscription.expires_at.isoformat()
                if subscription.expires_at
                else None,
            },
        )


def _end_date(subscription: Subscription) -> dict[str, datetime]:
    """`{end_date}` уведомления — сущностью date_time (0044)."""
    return {"end_date": subscription.expires_at} if subscription.expires_at else {}
