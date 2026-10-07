"""Главная и магазин: чек-лист, открытие и закрытие, состояние связей (1.7–1.15, 1.21, 1.22)."""

from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, PositiveInt
from sqlalchemy import func, select

from remnabay.access import LOGIN_TTL, SESSION_TTL
from remnabay.attention import attention
from remnabay.checklist import (
    Checklist,
    ChecklistItem,
    ItemKey,
    ItemStatus,
    ShopNotReadyError,
    build_checklist,
    close_shop,
    open_shop,
)
from remnabay.crypto import SecretBox, SecretDecryptionError
from remnabay.domain.panel_events import PanelEvent
from remnabay.domain.team import TeamMember, TeamRole
from remnabay.panel import PanelClient
from remnabay.panel_sync import panel_available, sync_page
from remnabay.queue import last_periodic_start
from remnabay.shop import MAX_TESTERS, SHOP_SUPPORT_CONTACT, SHOP_TESTERS, ShopState, shop_state
from remnabay.shop_settings import (
    SHOP_TIME_ZONE,
    SettingError,
    check_webhook_secret,
    get_setting,
    new_webhook_secret,
    set_setting,
    set_webhook_secret,
    webhook_secret,
)
from remnabay.web.admin._deps import AppSettings, DbSession, Member, Owner
from remnabay.web.panel_webhook import PANEL_WEBHOOK_PATH
from remnabay.worker.main import TASKS

router = APIRouter(tags=["shop"])


class PanelDetailsOut(BaseModel):
    version: str | None
    supported_versions: list[str]
    error: Literal["unavailable", "unauthorized", "error", "incompatible_version"] | None


class WebhookDetailsOut(BaseModel):
    url: str
    # Только владельцу: секрет задаётся в панели
    secret: str | None


class BrandDetailsOut(BaseModel):
    name: bool
    mark: bool


class ItemOut(BaseModel):
    key: ItemKey
    status: ItemStatus
    required: bool
    panel: PanelDetailsOut | None = None
    webhook: WebhookDetailsOut | None = None
    brand: BrandDetailsOut | None = None
    trial_enabled: bool | None = None


class ChecklistOut(BaseModel):
    state: ShopState
    can_open: bool
    # Невыполненных пунктов нет — чек-лист на главной не нужен (1.15)
    complete: bool
    items: list[ItemOut]


def _item_out(item: ChecklistItem) -> ItemOut:
    out = ItemOut(key=item.key, status=item.status, required=item.required)
    details = item.details
    match item.key:
        case ItemKey.PANEL:
            out.panel = PanelDetailsOut.model_validate(details)
        case ItemKey.WEBHOOK:
            out.webhook = WebhookDetailsOut.model_validate(details)
        case ItemKey.BRAND:
            out.brand = BrandDetailsOut.model_validate(details)
        case ItemKey.TRIAL:
            out.trial_enabled = bool(details.get("enabled"))
        case _:
            pass
    return out


def _checklist_out(checklist: Checklist) -> ChecklistOut:
    return ChecklistOut(
        state=checklist.state,
        can_open=checklist.can_open,
        complete=checklist.complete,
        items=[_item_out(item) for item in checklist.items],
    )


async def _checklist(
    request: Request, session: DbSession, settings: AppSettings, member: TeamMember
) -> Checklist:
    panel: PanelClient = request.app.state.panel
    box: SecretBox = request.app.state.box
    secret = None
    if member.role == TeamRole.OWNER:
        try:
            secret = await webhook_secret(session, box)
        except SecretDecryptionError:
            secret = None
    checklist = await build_checklist(
        session,
        panel,
        webhook_url=settings.public_link(PANEL_WEBHOOK_PATH),
        webhook_secret=secret,
    )
    await session.commit()
    return checklist


@router.get("/checklist")
async def checklist(
    request: Request, session: DbSession, settings: AppSettings, member: Member
) -> ChecklistOut:
    """Чек-лист первичной настройки (1.7)."""
    return _checklist_out(await _checklist(request, session, settings, member))


class NotReadyOut(BaseModel):
    missing: list[ItemKey]


@router.post(
    "/shop/open",
    responses={status.HTTP_409_CONFLICT: {"model": NotReadyOut}},
)
async def open_shop_endpoint(
    request: Request, session: DbSession, settings: AppSettings, owner: Owner
) -> ChecklistOut:
    """«Открыть магазин» — когда выполнены обязательные пункты (1.14)."""
    current = await _checklist(request, session, settings, owner)
    try:
        await open_shop(session, current, member_id=owner.id)
    except ShopNotReadyError as error:
        raise HTTPException(
            status.HTTP_409_CONFLICT, NotReadyOut(missing=error.missing).model_dump()
        ) from error
    await session.commit()
    return _checklist_out(await _checklist(request, session, settings, owner))


@router.post("/shop/close")
async def close_shop_endpoint(
    request: Request, session: DbSession, settings: AppSettings, owner: Owner
) -> ChecklistOut:
    """Временно закрыть магазин (1.22)."""
    await close_shop(session, member_id=owner.id)
    await session.commit()
    return _checklist_out(await _checklist(request, session, settings, owner))


class OverviewOut(BaseModel):
    """Состояние связей и «Требуют внимания» на главной (А1, 4.20, 4.30)."""

    state: ShopState
    panel_available: bool
    last_panel_event_at: datetime | None
    last_sync_at: datetime | None
    attention: int
    waiting_panel: int


@router.get("/overview")
async def overview(session: DbSession, member: Member) -> OverviewOut:
    del member
    needs = await attention(session, TASKS)
    return OverviewOut(
        state=await shop_state(session),
        panel_available=await panel_available(session),
        last_panel_event_at=await session.scalar(select(func.max(PanelEvent.received_at))),
        last_sync_at=await last_periodic_start(session, sync_page.name),
        attention=needs.count,
        waiting_panel=needs.waiting_panel,
    )


type _Contact = Annotated[str, Field(max_length=256)]


class ShopSettingsOut(BaseModel):
    support_contact: str
    time_zone: str
    testers: list[int]


class ShopSettingsIn(BaseModel):
    support_contact: _Contact
    time_zone: str
    testers: Annotated[list[PositiveInt], Field(max_length=MAX_TESTERS)]


async def _shop_settings(session: DbSession) -> ShopSettingsOut:
    return ShopSettingsOut(
        support_contact=await get_setting(session, SHOP_SUPPORT_CONTACT),
        time_zone=await get_setting(session, SHOP_TIME_ZONE),
        testers=await get_setting(session, SHOP_TESTERS),
    )


@router.get("/settings/shop", tags=["settings"])
async def shop_settings(session: DbSession, owner: Owner) -> ShopSettingsOut:
    del owner
    return await _shop_settings(session)


@router.put("/settings/shop", tags=["settings"])
async def update_shop_settings(
    body: ShopSettingsIn, session: DbSession, owner: Owner
) -> ShopSettingsOut:
    """Контакт поддержки (чек-лист), часовой пояс (1.18), тестировщики (1.21)."""
    testers = list(dict.fromkeys(body.testers))
    try:
        changes = [
            (SHOP_SUPPORT_CONTACT, body.support_contact.strip()),
            (SHOP_TIME_ZONE, SHOP_TIME_ZONE.parse(body.time_zone)),
        ]
    except SettingError as error:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Неизвестный часовой пояс"
        ) from error
    for setting, value in changes:
        if await get_setting(session, setting) != value:
            await set_setting(session, setting, value, member_id=owner.id)
    if await get_setting(session, SHOP_TESTERS) != testers:
        await set_setting(session, SHOP_TESTERS, testers, member_id=owner.id)
    await session.commit()
    return await _shop_settings(session)


class LoginSettingsOut(BaseModel):
    """«Вход в админку»: срок подтверждения входа (1.5) и срок сессии."""

    login_ttl_minutes: int
    session_ttl_days: int


class LoginSettingsIn(BaseModel):
    login_ttl_minutes: Annotated[int, Field(ge=1, le=60)]
    session_ttl_days: Annotated[int, Field(ge=1, le=365)]


async def _login_settings(session: DbSession) -> LoginSettingsOut:
    login_ttl = await get_setting(session, LOGIN_TTL)
    session_ttl = await get_setting(session, SESSION_TTL)
    return LoginSettingsOut(
        login_ttl_minutes=int(login_ttl.total_seconds() // 60),
        session_ttl_days=session_ttl.days,
    )


@router.get("/settings/login", tags=["settings"])
async def login_settings(session: DbSession, owner: Owner) -> LoginSettingsOut:
    del owner
    return await _login_settings(session)


@router.put("/settings/login", tags=["settings"])
async def update_login_settings(
    body: LoginSettingsIn, session: DbSession, owner: Owner
) -> LoginSettingsOut:
    for setting, value in (
        (LOGIN_TTL, timedelta(minutes=body.login_ttl_minutes)),
        (SESSION_TTL, timedelta(days=body.session_ttl_days)),
    ):
        if await get_setting(session, setting) != value:
            await set_setting(session, setting, value, member_id=owner.id)
    await session.commit()
    return await _login_settings(session)


class PanelSettingsOut(BaseModel):
    """«Панель»: адрес и секрет вебхука для панели (1.9)."""

    webhook_url: str
    # Нет — секрет не расшифровывается (сменили ENCRYPTION_KEY): задайте заново
    webhook_secret: str | None
    # Секрет подходит под правила панели; созданный до решения 0052 мог не подходить
    webhook_secret_fits_panel: bool


class WebhookSecretIn(BaseModel):
    webhook_secret: str


async def _panel_settings(
    request: Request, session: DbSession, settings: AppSettings
) -> PanelSettingsOut:
    box: SecretBox = request.app.state.box
    try:
        secret: str | None = await webhook_secret(session, box)
    except SecretDecryptionError:
        secret = None
    await session.commit()
    return PanelSettingsOut(
        webhook_url=settings.public_link(PANEL_WEBHOOK_PATH),
        webhook_secret=secret,
        webhook_secret_fits_panel=secret is not None and _fits_panel(secret),
    )


def _fits_panel(secret: str) -> bool:
    try:
        check_webhook_secret(secret)
    except SettingError:
        return False
    return True


@router.get("/settings/panel", tags=["settings"])
async def panel_settings(
    request: Request, session: DbSession, settings: AppSettings, owner: Owner
) -> PanelSettingsOut:
    del owner
    return await _panel_settings(request, session, settings)


@router.put("/settings/panel/webhook-secret", tags=["settings"])
async def update_webhook_secret(
    body: WebhookSecretIn,
    request: Request,
    session: DbSession,
    settings: AppSettings,
    owner: Owner,
) -> PanelSettingsOut:
    """Свой секрет — например, уже заданный в панели для прежнего бота (0052)."""
    box: SecretBox = request.app.state.box
    try:
        await set_webhook_secret(session, box, body.webhook_secret.strip(), member_id=owner.id)
    except SettingError as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    await session.commit()
    return await _panel_settings(request, session, settings)


@router.post("/settings/panel/webhook-secret/generate", tags=["settings"])
async def generate_webhook_secret(
    request: Request, session: DbSession, settings: AppSettings, owner: Owner
) -> PanelSettingsOut:
    """Новый секрет, созданный магазином: его нужно указать в панели."""
    box: SecretBox = request.app.state.box
    await set_webhook_secret(session, box, new_webhook_secret(), member_id=owner.id)
    await session.commit()
    return await _panel_settings(request, session, settings)
