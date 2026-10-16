"""План прогона и отчёт: что усыновить, кого перенести из файла (9.2, 9.4, 9.10–9.17).

Чистый расчёт без базы и панели: на входе снимок пользователей панели, то, что уже
есть в магазине, сопоставление тарифов и файл. Пробный и настоящий прогон считают
план одинаково (9.3): настоящий — по свежему снимку, и затем применяет его.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, Field

from remnabay.domain.tariffs import TrafficResetStrategy
from remnabay.migration._format import FileClient, MigrationFile, PanelUserRef
from remnabay.panel import User
from remnabay.panel_sync import STRATEGIES

# Сколько строк списка конфликтов и нераспознанного хранить в отчёте: числа считаются
# по всем, а список нужен, чтобы разобраться (решение 0071)
MAX_PROBLEMS = 5000
_CODE_LIMIT = 128
# Telegram ID хранится в BIGINT
MAX_TELEGRAM_ID = 2**63 - 1


class ProblemReason(StrEnum):
    # Конфликты (9.12): показываются и не применяются
    BOUND_ELSEWHERE = "bound_elsewhere"
    TELEGRAM_ID_MISMATCH = "telegram_id_mismatch"
    CLAIMED_TWICE = "claimed_twice"
    CODE_TAKEN = "code_taken"
    # Не распознано (9.11)
    DUPLICATE_TELEGRAM_ID = "duplicate_telegram_id"
    BAD_TELEGRAM_ID = "bad_telegram_id"
    PANEL_USER_NOT_FOUND = "panel_user_not_found"
    INVITER_NOT_FOUND = "inviter_not_found"
    SELF_INVITE = "self_invite"
    BAD_CODE = "bad_code"


class Problem(BaseModel):
    telegram_id: int | None = None
    panel_user: str | None = None
    reason: ProblemReason
    # Подробность: например, сам реферальный код
    detail: str | None = None


class MappingRule(BaseModel):
    """Группе «сквады и лимит устройств» — тариф (9.15)."""

    squads: list[UUID]
    device_limit: int | None
    tariff_id: int


class Squad(BaseModel):
    uuid: UUID
    name: str


class GroupReport(BaseModel):
    """Группа пользователей панели с одинаковыми сквадами и лимитом устройств."""

    squads: list[Squad]
    device_limit: int | None
    users: int
    tariff_id: int | None
    # Сколько подписок группы с параметрами не как у выбранного тарифа
    differs: int = 0
    # Подсказки адаптера: тариф старого бота → сколько пользователей
    hints: dict[str, int] = Field(default_factory=dict[str, int])


class ImportReport(BaseModel):
    """Что добавляет импорт файла (9.11)."""

    file_clients: int = 0
    links_found: int = 0
    links_not_found: int = 0
    # Связи с пользователями без Telegram ID в панели — усыновляются по файлу (0071)
    links_without_telegram_id: int = 0
    referral_links: int = 0
    referral_codes: int = 0
    # Клиенты без пользователя в панели: обычные, не усыновлённые (9.17)
    without_panel_user: int = 0
    # Уже были в магазине и ещё не переносились из файла: дополняются один раз (9.7)
    filled: int = 0
    # Уже перенесены из файла: пропускаются (9.7)
    skipped_imported: int = 0
    trial_used: int = 0
    paid_yes: int = 0
    paid_no: int = 0
    paid_unknown: int = 0


class MigrationReport(BaseModel):
    """Отчёт пробного прогона и итоговый отчёт (9.2, 9.11)."""

    panel_users: int = 0
    # Пользователей панели будет усыновлено
    adopt: int = 0
    # Клиентов с усыновлёнными подписками: новых и уже бывших в магазине (0071)
    clients: int = 0
    clients_new: int = 0
    clients_existing: int = 0
    clients_several: int = 0
    without_telegram_id: int = 0
    # Уже в магазине: усыновлены раньше или созданы самим магазином
    already_adopted: int = 0
    already_in_shop: int = 0
    no_tariff: int = 0
    differs: int = 0
    groups: list[GroupReport] = Field(default_factory=list[GroupReport])
    file: ImportReport | None = None
    conflicts: int = 0
    unrecognized: int = 0
    problems: list[Problem] = Field(default_factory=list[Problem])
    # Итоговый отчёт: что пропущено, потому что изменилось между планом и применением
    skipped: int = 0


class FileData(BaseModel):
    """Данные файла, которые записываются клиенту один раз (9.7, 9.17)."""

    username: str | None = None
    name: str | None = None
    language: str | None = None
    first_seen_at: datetime | None = None
    trial_used: bool = False
    paid: bool | None = None
    codes: list[str] = Field(default_factory=list[str])


class SubscriptionPlan(BaseModel):
    panel_user_id: int
    # Номер подписки у клиента: первая — название по умолчанию, дальше «Подписка N» (9.16)
    number: int
    tariff_id: int | None


class ClientPlan(BaseModel):
    telegram_id: int
    subscriptions: list[SubscriptionPlan] = Field(default_factory=list[SubscriptionPlan])
    # Дата появления нового клиента: из файла или самая ранняя дата создания в панели
    first_seen_at: datetime | None = None
    file: FileData | None = None


class InviteLink(BaseModel):
    invited: int
    inviter: int


class Plan(BaseModel):
    clients: list[ClientPlan] = Field(default_factory=list[ClientPlan])
    links: list[InviteLink] = Field(default_factory=list[InviteLink])


@dataclass(frozen=True)
class KnownClient:
    """Клиент, который уже есть в магазине."""

    client_id: int
    adopted: bool
    imported: bool
    subscriptions: int
    has_trial: bool
    has_inviter: bool


@dataclass(frozen=True)
class BoundUser:
    """Пользователь панели, уже привязанный к подписке магазина."""

    telegram_id: int | None
    adopted: bool


@dataclass(frozen=True)
class TariffParams:
    squads: frozenset[UUID]
    device_limit: int
    traffic_limit_bytes: int | None
    traffic_reset_strategy: TrafficResetStrategy | None


@dataclass(frozen=True)
class ShopState:
    """Что уже есть в магазине."""

    clients: dict[int, KnownClient] = field(default_factory=dict[int, KnownClient])
    bound: dict[int, BoundUser] = field(default_factory=dict[int, BoundUser])
    codes: dict[str, int] = field(default_factory=dict[str, int])
    tariffs: dict[int, TariffParams] = field(default_factory=dict[int, TariffParams])


type GroupKey = tuple[frozenset[UUID], int | None]


def group_key(user: User) -> GroupKey:
    return frozenset(squad.uuid for squad in user.active_internal_squads), user.hwid_device_limit


def _differs(user: User, tariff: TariffParams) -> bool:
    """Параметры в панели не как у тарифа: сквады, устройства, трафик (9.2, 0071)."""
    if group_key(user) != (tariff.squads, tariff.device_limit):
        return True
    if user.traffic_limit_bytes != (tariff.traffic_limit_bytes or 0):
        return True
    strategy = user.traffic_limit_strategy
    return strategy not in STRATEGIES or STRATEGIES[strategy] != tariff.traffic_reset_strategy


class _Builder:
    def __init__(
        self,
        users: list[User],
        shop: ShopState,
        mapping: list[MappingRule],
        file: MigrationFile | None,
    ) -> None:
        self.users = users
        self.shop = shop
        self.mapping: dict[GroupKey, int] = {
            (frozenset(rule.squads), rule.device_limit): rule.tariff_id
            for rule in mapping
            if rule.tariff_id in shop.tariffs
        }
        self.file = file
        self.by_id = {user.id: user for user in users}
        self.by_short_uuid = {user.short_uuid: user for user in users}
        self.by_username = {user.username: user for user in users}
        self.report = MigrationReport(panel_users=len(users))
        # В отчёт попадает, только если прогон с файлом
        self.file_report = ImportReport(file_clients=len(file.clients) if file else 0)
        self.problems: list[Problem] = []
        self.plan = Plan()

    def problem(self, reason: ProblemReason, **fields: str | int | None) -> None:
        conflict = reason in {
            ProblemReason.BOUND_ELSEWHERE,
            ProblemReason.TELEGRAM_ID_MISMATCH,
            ProblemReason.CLAIMED_TWICE,
            ProblemReason.CODE_TAKEN,
        }
        if conflict:
            self.report.conflicts += 1
        else:
            self.report.unrecognized += 1
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append(Problem.model_validate({"reason": reason, **fields}))

    # --- Файл: клиенты и связи с панелью ---

    def file_clients(self) -> dict[int, FileClient]:
        if self.file is None:
            return {}
        counts = Counter(client.telegram_id for client in self.file.clients)
        clients: dict[int, FileClient] = {}
        for client in self.file.clients:
            if not 0 < client.telegram_id <= MAX_TELEGRAM_ID:
                self.problem(ProblemReason.BAD_TELEGRAM_ID, telegram_id=client.telegram_id)
            elif counts[client.telegram_id] > 1:
                self.problem(ProblemReason.DUPLICATE_TELEGRAM_ID, telegram_id=client.telegram_id)
            else:
                clients[client.telegram_id] = client
        return clients

    def _find(self, ref: PanelUserRef) -> User | None:
        """По числовому ID, затем по `shortUuid`, затем по имени: хватает любого (0043)."""
        if ref.id is not None and ref.id in self.by_id:
            return self.by_id[ref.id]
        if ref.short_uuid is not None and ref.short_uuid in self.by_short_uuid:
            return self.by_short_uuid[ref.short_uuid]
        if ref.username is not None:
            return self.by_username.get(ref.username)
        return None

    def claims(self, clients: dict[int, FileClient]) -> tuple[dict[int, int], dict[int, str]]:
        """Связи файла: пользователь панели → Telegram ID; подсказки тарифа.

        Связь-конфликт не применяется (9.12): пользователь панели усыновляется так же,
        как без файла, — по Telegram ID в панели (0071)."""
        claimed: dict[int, int] = {}
        contested: set[int] = set()
        hints: dict[int, str] = {}
        for telegram_id, client in clients.items():
            for ref in client.panel_users:
                user = self._find(ref)
                report = self.file_report
                if user is None:
                    report.links_not_found += 1
                    self.problem(
                        ProblemReason.PANEL_USER_NOT_FOUND,
                        telegram_id=telegram_id,
                        panel_user=ref.label(),
                    )
                    continue
                report.links_found += 1
                if user.telegram_id is not None and user.telegram_id != telegram_id:
                    self.problem(
                        ProblemReason.TELEGRAM_ID_MISMATCH,
                        telegram_id=telegram_id,
                        panel_user=user.username,
                    )
                    continue
                owner = claimed.get(user.id)
                if owner is not None and owner != telegram_id:
                    contested.add(user.id)
                    self.problem(
                        ProblemReason.CLAIMED_TWICE,
                        telegram_id=telegram_id,
                        panel_user=user.username,
                    )
                    continue
                bound = self.shop.bound.get(user.id)
                if bound is not None and bound.telegram_id != telegram_id:
                    self.problem(
                        ProblemReason.BOUND_ELSEWHERE,
                        telegram_id=telegram_id,
                        panel_user=user.username,
                    )
                    continue
                claimed[user.id] = telegram_id
                if ref.tariff_hint:
                    hints[user.id] = ref.tariff_hint
        # Пользователь без Telegram ID в панели, указанный у двух клиентов, — ничей
        for user_id in contested:
            claimed.pop(user_id, None)
        self.file_report.links_without_telegram_id = sum(
            self.by_id[user_id].telegram_id is None for user_id in claimed
        )
        return claimed, hints

    # --- Усыновление: пользователи панели по клиентам ---

    def owners(self, claimed: dict[int, int]) -> dict[int, list[User]]:
        by_client: dict[int, list[User]] = defaultdict(list)
        for user in self.users:
            bound = self.shop.bound.get(user.id)
            if bound is not None:
                if bound.adopted:
                    self.report.already_adopted += 1
                else:
                    self.report.already_in_shop += 1
                continue
            telegram_id = claimed.get(user.id, user.telegram_id)
            if telegram_id is None or telegram_id <= 0:
                self.report.without_telegram_id += 1
                continue
            by_client[telegram_id].append(user)
        for users in by_client.values():
            # Порядок подписок — по дате создания пользователей в панели (9.16)
            users.sort(key=lambda user: (user.created_at, user.id))
        return by_client

    def groups(self, by_client: dict[int, list[User]], hints: dict[int, str]) -> None:
        groups: dict[GroupKey, GroupReport] = {}
        for users in by_client.values():
            for user in users:
                key = group_key(user)
                group = groups.get(key)
                if group is None:
                    names = {squad.uuid: squad.name for squad in user.active_internal_squads}
                    group = GroupReport(
                        squads=[
                            Squad(uuid=uuid, name=names[uuid])
                            for uuid in sorted(key[0], key=lambda uuid: names[uuid])
                        ],
                        device_limit=key[1],
                        users=0,
                        tariff_id=self.mapping.get(key),
                    )
                    groups[key] = group
                group.users += 1
                if group.tariff_id is None:
                    self.report.no_tariff += 1
                elif _differs(user, self.shop.tariffs[group.tariff_id]):
                    group.differs += 1
                    self.report.differs += 1
                hint = hints.get(user.id)
                if hint is not None:
                    group.hints[hint] = group.hints.get(hint, 0) + 1
        self.report.groups = sorted(groups.values(), key=lambda group: -group.users)

    # --- Клиенты ---

    def _codes(self, telegram_id: int, client: FileClient, known: KnownClient | None) -> list[str]:
        codes: list[str] = []
        for raw in dict.fromkeys(client.referral_codes):
            code = raw.strip()
            if not code or len(code) > _CODE_LIMIT:
                self.problem(ProblemReason.BAD_CODE, telegram_id=telegram_id, detail=raw[:64])
                continue
            owner = self.shop.codes.get(code)
            if owner is not None:
                if known is None or owner != known.client_id:
                    self.problem(ProblemReason.CODE_TAKEN, telegram_id=telegram_id, detail=code)
                continue
            codes.append(code)
        return codes

    def _file_data(
        self, telegram_id: int, client: FileClient, known: KnownClient | None
    ) -> FileData:
        report = self.file_report
        trial = client.trial_used and not (known is not None and known.has_trial)
        if client.trial_used:
            report.trial_used += 1
        if client.paid_in_old_bot is None:
            report.paid_unknown += 1
        elif client.paid_in_old_bot:
            report.paid_yes += 1
        else:
            report.paid_no += 1
        return FileData(
            username=client.username,
            name=client.name,
            language=client.language,
            first_seen_at=client.first_seen_at,
            trial_used=trial,
            paid=client.paid_in_old_bot,
            codes=self._codes(telegram_id, client, known),
        )

    def clients(self, by_client: dict[int, list[User]], files: dict[int, FileClient]) -> None:
        taken_codes: dict[str, int] = {}
        for telegram_id in sorted(by_client.keys() | files.keys()):
            users = by_client.get(telegram_id, [])
            known = self.shop.clients.get(telegram_id)
            client = files.get(telegram_id)
            data: FileData | None = None
            if client is not None:
                if known is not None and known.imported:
                    self.file_report.skipped_imported += 1
                else:
                    data = self._file_data(telegram_id, client, known)
                    data.codes = self._unique_codes(telegram_id, data.codes, taken_codes)
                    if known is not None:
                        self.file_report.filled += 1
                    elif not users:
                        self.file_report.without_panel_user += 1
                    self.file_report.referral_codes += len(data.codes)
            if not users and data is None:
                continue
            existing = known.subscriptions if known is not None else 0
            subscriptions = [
                SubscriptionPlan(
                    panel_user_id=user.id,
                    number=existing + index + 1,
                    tariff_id=self.mapping.get(group_key(user)),
                )
                for index, user in enumerate(users)
            ]
            if users:
                self.report.adopt += len(users)
                self.report.clients += 1
                if known is None:
                    self.report.clients_new += 1
                else:
                    self.report.clients_existing += 1
                if existing + len(users) > 1:
                    self.report.clients_several += 1
            # Дата появления — из файла, иначе самая ранняя дата создания в панели (0071)
            first_seen = data.first_seen_at if data is not None else None
            if first_seen is None and users:
                first_seen = min(user.created_at for user in users)
            self.plan.clients.append(
                ClientPlan(
                    telegram_id=telegram_id,
                    subscriptions=subscriptions,
                    first_seen_at=first_seen,
                    file=data,
                )
            )

    def _unique_codes(self, telegram_id: int, codes: list[str], taken: dict[str, int]) -> list[str]:
        unique: list[str] = []
        for code in codes:
            owner = taken.setdefault(code, telegram_id)
            if owner != telegram_id:
                self.problem(ProblemReason.CODE_TAKEN, telegram_id=telegram_id, detail=code)
            else:
                unique.append(code)
        return unique

    def links(self, files: dict[int, FileClient]) -> None:
        """Пригласившие из файла (9.11); существующая связь не меняется (0071).

        Клиенту, уже перенесённому из файла, связь тоже ставится, если её нет: прогон,
        прервавшийся до последнего шага, не записал связи, а повтор их допишет (9.7)."""
        planned = {item.telegram_id: item for item in self.plan.clients}
        for telegram_id, client in files.items():
            item = planned.get(telegram_id)
            known = self.shop.clients.get(telegram_id)
            if client.invited_by is None or (item is None and known is None):
                continue
            if known is not None and known.has_inviter:
                continue
            # Уже перенесённого клиента в отчёте не разбираем второй раз
            reported = item is not None and item.file is not None
            if client.invited_by == telegram_id:
                if reported:
                    self.problem(ProblemReason.SELF_INVITE, telegram_id=telegram_id)
                continue
            if client.invited_by not in planned and client.invited_by not in self.shop.clients:
                if not reported:
                    continue
                self.problem(
                    ProblemReason.INVITER_NOT_FOUND,
                    telegram_id=telegram_id,
                    detail=str(client.invited_by),
                )
                continue
            self.plan.links.append(InviteLink(invited=telegram_id, inviter=client.invited_by))
            self.file_report.referral_links += 1


def make_plan(
    users: list[User],
    shop: ShopState,
    mapping: list[MappingRule],
    file: MigrationFile | None = None,
) -> tuple[Plan, MigrationReport]:
    """План и отчёт прогона. Пользователи панели вне файла усыновляются так же (9.10)."""
    builder = _Builder(users, shop, mapping, file)
    files = builder.file_clients()
    claimed, hints = builder.claims(files)
    by_client = builder.owners(claimed)
    builder.groups(by_client, hints)
    builder.clients(by_client, files)
    builder.links(files)
    builder.report.problems = builder.problems
    if file is not None:
        builder.report.file = builder.file_report
    return builder.plan, builder.report
