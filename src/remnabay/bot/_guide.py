"""С2. Инструкция по подключению — rich, из конфига Subscription Page (блок 12, решение 0019).

- Конфиг — назначенный подписке в панели; панель не ответила — последний известный
  (12.1, 12.8). Конфиг ни разу не получен — `guide.unavailable` (12.8, 12.16).
- Платформы — сворачиваемые блоки; внутри рекомендуемое приложение и его шаги,
  остальные — по «Другие приложения» (12.3). Тексты — на языке клиента (12.2).
- Кнопки шагов: копирование — ссылка подписки, внешняя ссылка — как есть, импорт в
  приложение — через страницу-переходник (12.4, 12.10). SVG-иконки и брендинг
  веб-страницы не переносятся (12.5).
- Под сообщением: «Скопировать ссылку», «Открыть в браузере» — только если панель
  пускает браузер на страницу подписки (12.6, 12.16), «Главное меню».
"""

import asyncio
from itertools import batched

from aiogram import Router
from aiogram.types import (
    CallbackQuery,
    CopyTextButton,
    InputRichBlockButtons,
    InputRichBlockDetails,
    InputRichBlockParagraph,
    InputRichBlockSectionHeading,
    InputRichBlockUnion,
    InputRichMessage,
    RichMessageButton,
    RichTextBold,
)

from remnabay.bot._common import PANEL_TIMEOUT, Guide, GuideApps, own_subscription, show
from remnabay.bot._context import BotContext
from remnabay.bot._screens import Keyboard, Screen
from remnabay.bot._shop import main_menu
from remnabay.domain.subscriptions import Subscription
from remnabay.guide import (
    REDIRECT_PATH,
    ButtonType,
    ConfigApp,
    ConfigButton,
    GuideConfig,
    Language,
    assign_config,
    fetch_config,
    known_config_uuid,
    redirect_token,
    stored_config,
    webpage_allowed,
)
from remnabay.messaging import MAIN_MENU_CALLBACK
from remnabay.panel import PanelError
from remnabay.shop_settings import SHOP_LANGUAGE, get_setting

# Кнопок в одном ряду rich-сообщения — не больше 8 (Bot API)
_ROW = 8
_WEB_SCHEMES = ("https://", "http://", "tg://")


async def _config(ctx: BotContext, subscription: Subscription) -> GuideConfig | None:
    """Конфиг подписки: назначение — у панели, содержимое — сохранённое; ещё не
    сохранённое — у панели. Панель не ответила — последнее известное (12.8)."""
    try:
        async with asyncio.timeout(PANEL_TIMEOUT.total_seconds()):
            config_uuid = await assign_config(ctx.session, ctx.panel, subscription)
    except PanelError, TimeoutError:
        config_uuid = known_config_uuid(subscription)
    config = await stored_config(ctx.session, config_uuid)
    if config is not None:
        return config
    try:
        async with asyncio.timeout(PANEL_TIMEOUT.total_seconds()):
            await fetch_config(ctx.session, ctx.panel, config_uuid)
    except PanelError, TimeoutError:
        return None
    return await stored_config(ctx.session, config_uuid)


class _Builder:
    """Собирает rich-инструкцию одной подписки."""

    def __init__(
        self, ctx: BotContext, subscription: Subscription, config: GuideConfig, language: Language
    ) -> None:
        self._ctx = ctx
        self._subscription = subscription
        self._config = config
        self._language = language

    async def _button(
        self, platform: str, app: ConfigApp, button: ConfigButton
    ) -> RichMessageButton | None:
        text = self._language.pick(button.text) or app.name
        match button.type:
            case ButtonType.COPY_BUTTON:
                link = self._subscription.subscription_url
                return RichMessageButton(text=text, copy_text=CopyTextButton(text=link))
            case ButtonType.EXTERNAL if button.link.startswith(_WEB_SCHEMES):
                return RichMessageButton(text=text, url=button.link)
            case ButtonType.SUBSCRIPTION_LINK if button.link:
                token = await redirect_token(
                    self._ctx.session, self._subscription, platform, app.name, button.link
                )
                return RichMessageButton(
                    text=text, url=f"{self._ctx.shop_url}{REDIRECT_PATH}/{token}"
                )
            case _:
                # Неизвестный вид кнопки или ссылка, которую Telegram не откроет
                return None

    async def app(self, platform: str, app: ConfigApp) -> list[InputRichBlockUnion]:
        """Приложение и его шаги: заголовок, описание, кнопки (12.3, 12.4)."""
        blocks: list[InputRichBlockUnion] = [InputRichBlockSectionHeading(text=app.name, size=4)]
        for step in app.blocks:
            title = self._language.pick(step.title)
            description = self._language.pick(step.description)
            if title:
                blocks.append(InputRichBlockParagraph(text=RichTextBold(text=title)))
            if description:
                blocks.append(InputRichBlockParagraph(text=description))
            buttons = [
                found
                for button in step.buttons
                if (found := await self._button(platform, app, button)) is not None
            ]
            rows = batched(buttons, _ROW, strict=False)
            blocks.extend(InputRichBlockButtons(buttons=list(row)) for row in rows)
        return blocks

    async def message(self, expanded: str | None) -> InputRichMessage | None:
        """Платформы — сворачиваемые блоки. У платформы `expanded` блок раскрыт и
        показаны все её приложения (12.3). `None` — платформ с приложениями нет."""
        blocks: list[InputRichBlockUnion] = []
        header = self._language.pick(self._config.base_translations.installation_guide_header)
        if header:
            blocks.append(InputRichBlockSectionHeading(text=header, size=3))
        for key, platform in self._config.platforms.items():
            recommended = platform.recommended()
            if recommended is None:
                continue
            content = await self.app(key, recommended)
            others = [app for app in platform.apps if app is not recommended]
            if key == expanded:
                for app in others:
                    content.extend(await self.app(key, app))
            elif others:
                more = GuideApps(subscription_id=self._subscription.id, platform=key)
                content.append(
                    InputRichBlockButtons(
                        buttons=[
                            RichMessageButton(
                                text=self._ctx.render("btn.other_apps"), callback_data=more.pack()
                            )
                        ]
                    )
                )
            summary = self._language.pick(platform.display_name) or key
            blocks.append(
                InputRichBlockDetails(
                    summary=RichTextBold(text=summary), blocks=content, is_open=key == expanded
                )
            )
        if len(blocks) == (1 if header else 0):
            return None
        return InputRichMessage(blocks=blocks)


async def guide_screen(
    ctx: BotContext, subscription: Subscription, expanded: str | None = None
) -> Screen:
    """С2 или, если конфиг ни разу не получен, `guide.unavailable` (12.8)."""
    # Сначала конфиг: вместе с ним панель отвечает, пускает ли она браузер (12.16)
    config = await _config(ctx, subscription)
    browser = await webpage_allowed(ctx.session)
    keyboard = Keyboard().copy(ctx.render("btn.copy_link"), subscription.subscription_url)
    if browser:
        keyboard.link(ctx.render("btn.open_in_browser"), subscription.subscription_url)
    keyboard.action(ctx.render("btn.main_menu"), MAIN_MENU_CALLBACK)
    rich = None
    if config is not None:
        default = await get_setting(ctx.session, SHOP_LANGUAGE)
        language = Language(config, ctx.client.language_code, default)
        rich = await _Builder(ctx, subscription, config, language).message(expanded)
    if rich is None:
        key = "guide.unavailable" if browser else "guide.unavailable_no_browser"
        return Screen.plain(ctx.render(key), keyboard)
    return Screen(keyboard=keyboard, rich=rich)


async def open_guide(query: CallbackQuery, callback_data: Guide, ctx: BotContext) -> None:
    """«Как подключить» — инструкция отдельным сообщением: событие не перезаписывает меню."""
    await query.answer()
    subscription = await own_subscription(ctx, callback_data.subscription_id)
    if subscription is None:
        await (await main_menu(ctx)).send(ctx.bot, query.from_user.id)
        return
    await (await guide_screen(ctx, subscription)).send(ctx.bot, query.from_user.id)


async def other_apps(query: CallbackQuery, callback_data: GuideApps, ctx: BotContext) -> None:
    """«Другие приложения» — та же инструкция с раскрытой платформой (12.3)."""
    subscription = await own_subscription(ctx, callback_data.subscription_id)
    if subscription is None:
        await query.answer()
        return
    await show(query, ctx, await guide_screen(ctx, subscription, callback_data.platform))


def build_router() -> Router:
    router = Router(name="guide")
    router.callback_query.register(open_guide, Guide.filter())
    router.callback_query.register(other_apps, GuideApps.filter())
    return router
