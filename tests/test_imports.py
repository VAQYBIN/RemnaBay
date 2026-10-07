"""Каждый пакет магазина импортируется сам по себе, в чистом процессе.

Круговой импорт проявляется только при определённом порядке импорта: в общем
прогоне тестов модули уже загружены, и ошибка не видна.
"""

import asyncio
import sys

import pytest

ENTRY_POINTS = (
    "remnabay.runtime",
    "remnabay.messaging",
    "remnabay.payments",
    "remnabay.panel_sync",
    "remnabay.attention",
    "remnabay.queue",
    "remnabay.access",
    "remnabay.bot",
    "remnabay.clients",
    "remnabay.shop",
    "remnabay.brand",
    "remnabay.checklist",
    "remnabay.stats",
    "remnabay.web.admin",
    "remnabay.web.app",
    "remnabay.worker.main",
)


async def _import_alone(module: str) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        f"import {module}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await process.communicate()
    return process.returncode or 0, stderr.decode()


async def test_every_package_imports_on_its_own() -> None:
    results = await asyncio.gather(*(_import_alone(module) for module in ENTRY_POINTS))

    failures = {
        module: error.strip().splitlines()[-1]
        for module, (code, error) in zip(ENTRY_POINTS, results, strict=True)
        if code != 0
    }
    if failures:
        pytest.fail(f"Модули не импортируются сами по себе: {failures}")
