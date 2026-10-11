"""Конфиг Subscription Page и запуск задач очереди для тестов этапа 7 (блоки 5, 6, 12).

Конфиг небольшой, в формате панели 3.4.4 (версия конфига «1»), написан для тестов:
у iOS два рекомендуемых приложения и одно обычное, у Linux рекомендуемых нет.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from remnabay.queue import TaskContext, TaskDefinition
from remnabay.queue._models import QueueTask, TaskStatus

SQUAD_CONFIG = "11111111-2222-3333-4444-555555555555"


def _text(ru: str, en: str | None = None) -> dict[str, str]:
    return {"ru": ru, "en": en or f"{ru} (en)"}


def guide_config(**overrides: Any) -> dict[str, Any]:
    """Содержимое конфига Subscription Page."""
    happ_ios = {
        "name": "Happ",
        "featured": True,
        "svgIconKey": "Happ",
        "blocks": [
            {
                "title": _text("Установка приложения", "App installation"),
                "description": _text("Установите приложение из App Store.", "Install the app."),
                "svgIconKey": "Download",
                "svgIconColor": "violet",
                "buttons": [
                    {
                        "link": "https://apps.apple.com/app/happ",
                        "type": "external",
                        "text": _text("Открыть в App Store", "Open in App Store"),
                        "svgIconKey": "ExternalLink",
                    }
                ],
            },
            {
                "title": _text("Добавление подписки", "Add subscription"),
                "description": _text("Нажмите кнопку ниже.", "Press the button below."),
                "svgIconKey": "Cloud",
                "svgIconColor": "cyan",
                "buttons": [
                    {
                        "link": "happ://add/{{SUBSCRIPTION_LINK}}",
                        "type": "subscriptionLink",
                        "text": _text("Добавить подписку", "Add subscription"),
                        "svgIconKey": "Plus",
                    },
                    {
                        "link": "{{SUBSCRIPTION_LINK}}",
                        "type": "copyButton",
                        "text": _text("Скопировать", "Copy"),
                        "svgIconKey": "Copy",
                    },
                ],
            },
        ],
    }
    stash = {
        "name": "Stash",
        "featured": True,
        "blocks": [
            {
                "title": _text("Добавление"),
                "description": _text("Добавьте подписку в Stash."),
                "svgIconKey": "Cloud",
                "svgIconColor": "cyan",
                "buttons": [
                    {
                        "link": "stash://install-config?url={{SUBSCRIPTION_LINK}}&name={{USERNAME}}",
                        "type": "subscriptionLink",
                        "text": _text("В Stash"),
                        "svgIconKey": "Plus",
                    }
                ],
            }
        ],
    }
    streisand: dict[str, Any] = {"name": "Streisand", "featured": False, "blocks": []}
    flclash = {
        "name": "FlClashX",
        "featured": False,
        "blocks": [
            {
                "title": _text("Установка FlClashX"),
                "description": _text("Скачайте пакет."),
                "svgIconKey": "Download",
                "svgIconColor": "violet",
                "buttons": [
                    {
                        "link": "https://github.com/flclashx/releases",
                        "type": "external",
                        "text": _text("Скачать"),
                        "svgIconKey": "ExternalLink",
                    },
                    {
                        "link": "flclashx://install-config?url={{SUBSCRIPTION_LINK}}",
                        "type": "subscriptionLink",
                        "text": _text("Добавить в FlClashX"),
                        "svgIconKey": "Plus",
                    },
                ],
            }
        ],
    }
    koala: dict[str, Any] = {"name": "Koala Clash", "featured": False, "blocks": []}
    config: dict[str, Any] = {
        "version": "1",
        "locales": ["ru", "en"],
        "brandingSettings": {"title": "Sub", "logoUrl": "https://x/logo.svg", "supportUrl": "x"},
        "baseTranslations": {"installationGuideHeader": _text("Установка", "Installation")},
        "svgLibrary": {"Happ": "<svg/>"},
        "platforms": {
            "ios": {
                "displayName": _text("iOS"),
                "svgIconKey": "Apple",
                "apps": [happ_ios, stash, streisand],
            },
            "linux": {
                "displayName": _text("Linux"),
                "svgIconKey": "Linux",
                "apps": [flclash, koala],
            },
        },
    }
    config.update(overrides)
    return config


async def queued(session: AsyncSession, definition: TaskDefinition[Any]) -> list[QueueTask]:
    """Ждущие задачи вида по порядку постановки."""
    rows = await session.scalars(
        select(QueueTask)
        .where(QueueTask.name == definition.name, QueueTask.status == TaskStatus.PENDING)
        .order_by(QueueTask.id)
    )
    return list(rows)


async def run_queued(session: AsyncSession, definition: TaskDefinition[Any]) -> int:
    """Выполняет ждущие задачи вида в транзакции теста (окружение задаёт `fake_runtime`)."""
    rows = await queued(session, definition)
    for task in rows:
        context = TaskContext(session=session, task_id=task.id, attempt_number=1)
        await definition.run(context, task.args)
        task.status = TaskStatus.DONE
    await session.flush()
    return len(rows)
