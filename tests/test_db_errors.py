"""Какие ошибки значат «база не отвечает» (решение 0076): по ним в логе одна строка."""

import psycopg
import pytest
from psycopg import errors
from sqlalchemy.exc import OperationalError, ProgrammingError

from remnabay.db import database_unavailable, describe_error


def _wrapped(original: Exception) -> OperationalError:
    return OperationalError("SELECT 1", None, original)


@pytest.mark.parametrize(
    "original",
    [
        # Нет соединения: кода SQLSTATE нет
        psycopg.OperationalError("connection failed: Connection refused"),
        # База останавливается или ещё запускается
        errors.AdminShutdown("terminating connection due to administrator command"),
        errors.CannotConnectNow("the database system is starting up"),
        # Потеряно соединение
        errors.ConnectionFailure("connection failure"),
    ],
)
def test_0076_connection_problems_mean_database_unavailable(original: Exception) -> None:
    assert database_unavailable(_wrapped(original))


@pytest.mark.parametrize("error", [ConnectionRefusedError(), TimeoutError()])
def test_0076_network_errors_mean_database_unavailable(error: Exception) -> None:
    assert database_unavailable(error)


@pytest.mark.parametrize(
    "error",
    [
        # Ответ работающей базы — не недоступность
        _wrapped(errors.DeadlockDetected("deadlock detected")),
        ProgrammingError("SELECT x", None, errors.UndefinedColumn("column x does not exist")),
        ValueError("не про базу"),
    ],
)
def test_0076_other_errors_are_not_unavailability(error: Exception) -> None:
    assert not database_unavailable(error)


def test_0076_error_described_in_one_line() -> None:
    error = _wrapped(psycopg.OperationalError("connection failed: refused\nIs the server running?"))

    assert describe_error(error) == "OperationalError: connection failed: refused"
