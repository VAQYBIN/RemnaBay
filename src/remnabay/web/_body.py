"""Чтение тела запроса с пределом размера: слишком большое не читается целиком."""

from fastapi import HTTPException, Request, status


async def read_limited(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Файл слишком большой")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Файл слишком большой")
    return bytes(body)
