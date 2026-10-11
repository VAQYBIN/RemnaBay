"""Инструкция по подключению из конфига Subscription Page и страница-переходник (блок 12,
решения 0019, 0043, 0058).

Своих полей инструкции у магазина нет (12.9): оператор правит её в панели, а
магазин хранит последний полученный конфиг и собирает из него инструкцию в боте.
"""

from remnabay.guide._config import (
    ButtonType,
    ConfigApp,
    ConfigBlock,
    ConfigButton,
    ConfigError,
    ConfigPlatform,
    GuideConfig,
    Language,
    parse_config,
)
from remnabay.guide._redirect import (
    REDIRECT_PATH,
    Redirect,
    RedirectLink,
    deeplink,
    redirect_token,
    resolve,
)
from remnabay.guide._store import (
    BROWSER_HEADERS,
    DEFAULT_CONFIG_UUID,
    REFRESH_CONFIGS,
    WEBPAGE_ALLOWED,
    RefreshResult,
    StoredConfig,
    assign_config,
    fetch_config,
    known_config_uuid,
    last_refreshed_at,
    refresh_configs,
    refresh_configs_task,
    remember_assignment,
    store_config,
    stored_config,
    webpage_allowed,
)

__all__ = [
    "BROWSER_HEADERS",
    "DEFAULT_CONFIG_UUID",
    "REDIRECT_PATH",
    "REFRESH_CONFIGS",
    "WEBPAGE_ALLOWED",
    "ButtonType",
    "ConfigApp",
    "ConfigBlock",
    "ConfigButton",
    "ConfigError",
    "ConfigPlatform",
    "GuideConfig",
    "Language",
    "Redirect",
    "RedirectLink",
    "RefreshResult",
    "StoredConfig",
    "assign_config",
    "deeplink",
    "fetch_config",
    "known_config_uuid",
    "last_refreshed_at",
    "parse_config",
    "redirect_token",
    "refresh_configs",
    "refresh_configs_task",
    "remember_assignment",
    "resolve",
    "store_config",
    "stored_config",
    "webpage_allowed",
]
