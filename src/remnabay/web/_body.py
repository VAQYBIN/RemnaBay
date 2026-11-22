"""Чтение тела запроса с пределом размера: слишком большое не читается целиком."""

from fastapi import HTTPException, Request, status

_MEGABYTE = 1024 * 1024


def too_large_message(limit: int) -> str:
    """«Файл больше 1 МБ»: оператор видит предел, а не только отказ (решение 0076)."""
    if limit % _MEGABYTE == 0:
        return f"Файл больше {limit // _MEGABYTE} МБ"
    return f"Файл больше {limit // 1024} КБ"


async def read_limited(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, too_large_message(limit))
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, too_large_message(limit))
    return bytes(body)
