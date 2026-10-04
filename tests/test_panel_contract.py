"""Контрактный тест клиента панели (решения 0027, 0042).

Вызовы и модели клиента сверяются с `openapi.json` каждой поддерживаемой версии
панели: пути и методы, параметры пути, поля запросов и ответов, их типы,
обязательность, обнуляемость и значения перечислений. Модели вебхуков сверяются
с моделями событий из того же файла.

Файл берётся из образа панели: `scripts/fetch-panel-openapi.sh`.
"""

import json
import os
import re
import types
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, TypeAliasType, Union, get_args, get_origin
from uuid import UUID

import pytest
from pydantic import AwareDatetime, BaseModel

from remnabay.panel import ENDPOINTS, SUPPORTED_PANEL_VERSIONS, required_scopes
from remnabay.panel._models import PanelModel, PanelRequest
from remnabay.panel._webhooks import DeviceEvent, UserEvent

ROOT = Path(__file__).resolve().parents[1]
OPENAPI_DIR = Path(os.environ.get("PANEL_OPENAPI_DIR", ROOT / ".cache" / "panel-openapi"))
UNKNOWN = "UNKNOWN"

type Schema = dict[str, Any]


class Spec:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.schemas: dict[str, Schema] = raw["components"]["schemas"]

    def resolve(self, schema: Schema) -> Schema:
        while "$ref" in schema:
            schema = self.schemas[schema["$ref"].rsplit("/", 1)[-1]]
        return schema

    def operation(self, method: str, path: str) -> Schema | None:
        return self.raw["paths"].get(path, {}).get(method.lower())


@pytest.fixture(scope="module", params=SUPPORTED_PANEL_VERSIONS)
def spec(request: pytest.FixtureRequest) -> Spec:
    version: str = request.param
    path = OPENAPI_DIR / f"{version}.json"
    if not path.exists():
        pytest.fail(
            f"Нет контракта панели {version} ({path}). "
            f"Получите его: scripts/fetch-panel-openapi.sh {version}"
        )
    return Spec(json.loads(path.read_text()))


# --- Разбор аннотаций моделей ---


def _unwrap(annotation: Any) -> Any:
    while True:
        if isinstance(annotation, TypeAliasType):
            annotation = annotation.__value__
        elif get_origin(annotation) is Annotated:
            annotation = get_args(annotation)[0]
        else:
            return annotation


def _split_optional(annotation: Any) -> tuple[Any, bool]:
    """Тип без `None` и признак, что `None` допустим."""
    annotation = _unwrap(annotation)
    if get_origin(annotation) in (Union, types.UnionType):
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        optional = len(args) < len(get_args(annotation))
        inner = args[0] if len(args) == 1 else annotation
        return _unwrap(inner), optional
    return annotation, False


def _enum_values(enum_class: type[StrEnum]) -> set[str]:
    return {member.value for member in enum_class} - {UNKNOWN}


def _check_type(
    spec: Spec, annotation: Any, prop: Schema, where: str, problems: list[str], request: bool
) -> None:
    prop = spec.resolve(prop)
    kind = prop.get("type")
    if annotation is AwareDatetime:
        annotation = datetime
    origin = get_origin(annotation)
    if kind is None:
        return  # в контракте тип не задан — подходит любой
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        contract_values = set(prop.get("enum", []))
        model_values = _enum_values(annotation)
        if kind != "string" or not contract_values:
            problems.append(f"{where}: в контракте не перечисление строк")
        elif request and not model_values <= contract_values:
            problems.append(f"{where}: панель не примет {sorted(model_values - contract_values)}")
        elif not request and contract_values != model_values:
            problems.append(
                f"{where}: значения перечисления разошлись: в контракте {sorted(contract_values)},"
                f" в модели {sorted(model_values)}"
            )
        return
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if kind != "object":
            problems.append(f"{where}: в контракте {kind}, а не объект")
            return
        _check_model(spec, annotation, prop, where, problems, request=request)
        return
    if origin is list:
        if kind != "array":
            problems.append(f"{where}: в контракте {kind}, а не массив")
            return
        item, _ = _split_optional(get_args(annotation)[0])
        _check_type(spec, item, prop.get("items", {}), f"{where}[]", problems, request)
        return
    expected: dict[Any, set[str]] = {
        int: {"integer", "number"},
        float: {"number"},
        str: {"string"},
        bool: {"boolean"},
        datetime: {"string"},
        UUID: {"string"},
    }
    if origin is dict:
        allowed = {"object"}
    elif annotation in expected:
        allowed = expected[annotation]
    else:
        problems.append(f"{where}: тип модели {annotation} не умеем сверять")
        return
    if kind not in allowed:
        problems.append(f"{where}: в контракте {kind}, в модели {annotation}")
    if annotation is datetime and not _accepts_datetime(prop):
        problems.append(f"{where}: в контракте не дата-время")


# Как модель отправляет дату: ISO 8601 с часовым поясом
_SERIALIZED_DATETIMES = ("2026-10-05T12:00:00Z", "2026-10-05T12:00:00.123456+05:00")


def _accepts_datetime(prop: Schema) -> bool:
    """Дата-время в контракте: формат `date-time` или шаблон, под который подходит
    наша сериализация (в запросах панель описывает дату шаблоном)."""
    if prop.get("format") == "date-time":
        return True
    pattern = prop.get("pattern")
    return pattern is not None and all(re.fullmatch(pattern, v) for v in _SERIALIZED_DATETIMES)


def _check_model(
    spec: Spec,
    model: type[BaseModel],
    schema: Schema,
    where: str,
    problems: list[str],
    *,
    request: bool,
) -> None:
    schema = spec.resolve(schema)
    props: dict[str, Schema] = schema.get("properties", {})
    required = set(schema.get("required", []))
    nullable_fields: frozenset[str] = (
        model.NULLABLE if issubclass(model, PanelRequest) else frozenset()
    )

    for name, field in model.model_fields.items():
        alias = field.alias or name
        path = f"{where}.{alias}"
        if alias not in props:
            problems.append(f"{path}: поля нет в контракте")
            continue
        prop = spec.resolve(props[alias])
        annotation, optional = _split_optional(field.annotation)
        if request:
            if name in nullable_fields and not prop.get("nullable"):
                problems.append(f"{path}: модель отправляет null, а панель его не принимает")
        elif not optional and (alias not in required or prop.get("nullable")):
            problems.append(f"{path}: в контракте может отсутствовать или быть null")
        _check_type(spec, annotation, prop, path, problems, request)

    if request:
        model_required = {
            field.alias or name for name, field in model.model_fields.items() if field.is_required()
        }
        missing = required - model_required
        if missing:
            problems.append(
                f"{where}: обязательные поля контракта не обязательны в модели: {missing}"
            )


def _json_schema(operation: Schema, *keys: str) -> Schema:
    node: Any = operation
    for key in keys:
        node = node[key]
    return node["content"]["application/json"]["schema"]


# --- Запросы клиента ---


@pytest.mark.parametrize("endpoint", ENDPOINTS, ids=lambda e: f"{e.method} {e.path}")
def test_endpoint_matches_panel_contract(spec: Spec, endpoint: Any) -> None:
    """Путь, метод, параметры пути, тело запроса и ответ — как в контракте панели."""
    operation = spec.operation(endpoint.method, endpoint.path)
    assert operation is not None, f"В контракте нет {endpoint.method} {endpoint.path}"
    problems: list[str] = []

    path_params = set(re.findall(r"{(\w+)}", endpoint.path))
    contract_params = {p["name"] for p in operation.get("parameters", []) if p["in"] == "path"}
    if path_params != contract_params:
        problems.append(f"параметры пути: {path_params} против {contract_params}")

    if endpoint.request is not None:
        assert "requestBody" in operation, "контракт не ждёт тела запроса"
        body = _json_schema(operation, "requestBody")
        _check_model(spec, endpoint.request, body, "запрос", problems, request=True)

    success = next(code for code in ("200", "201") if code in operation["responses"])
    envelope = spec.resolve(_json_schema(operation, "responses", success))
    _check_model(
        spec,
        endpoint.response,
        envelope["properties"]["response"],
        "ответ",
        problems,
        request=False,
    )

    assert not problems, "\n".join(problems)


def test_list_users_pagination_parameters(spec: Spec) -> None:
    """Сверка всех подписок и усыновление листают пользователей по `start` и `size`."""
    operation = spec.operation("GET", "/api/users")
    assert operation is not None
    query = {p["name"] for p in operation["parameters"] if p["in"] == "query"}
    assert {"start", "size"} <= query


# --- Вебхуки ---


@pytest.mark.parametrize(
    ("model", "schema_name"),
    [
        (UserEvent, "RemnawaveWebhookUserEventsDto"),
        (DeviceEvent, "RemnawaveWebhookUserHwidDevicesEventsDto"),
    ],
    ids=["user", "user_hwid_devices"],
)
def test_webhook_event_models_match_panel_contract(
    spec: Spec, model: type[PanelModel], schema_name: str
) -> None:
    """Модели событий панели, которые разбирает магазин, — как в контракте."""
    problems: list[str] = []

    _check_model(spec, model, spec.schemas[schema_name], "событие", problems, request=False)

    assert not problems, "\n".join(problems)


def test_required_scopes_are_listed_per_endpoint() -> None:
    """Права API-токена для документации оператора: ресурс и действие у каждого запроса."""
    scopes = required_scopes()

    assert len(scopes) == len(ENDPOINTS)
    assert all(re.fullmatch(r"[a-z-]+:[a-z-]+", scope) for scope in scopes)
