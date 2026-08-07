"""CLOB REST helpers."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import settings


def levels_from_book(raw: dict[str, Any], side: str) -> list[dict[str, float]]:
    key = "bids" if side == "bids" else "asks"
    rows = raw.get(key) or []
    out: list[dict[str, float]] = []
    for row in rows:
        try:
            price = float(row.get("price"))
            size = float(row.get("size") or row.get("shares") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if size <= 0:
            continue
        out.append({"price": price, "size": size})
    reverse = side == "bids"
    out.sort(key=lambda x: x["price"], reverse=reverse)
    return out


class ClobRest:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=settings.clob_url,
            timeout=httpx.Timeout(8.0, connect=4.0),
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def get_book(self, token_id: str) -> dict[str, Any]:
        resp = await self._http.get("/book", params={"token_id": token_id})
        resp.raise_for_status()
        return resp.json()

    async def seed_books(
        self, token_up: str | None, token_down: str | None
    ) -> tuple[dict[str, list[dict[str, float]]], dict[str, list[dict[str, float]]]]:
        up: dict[str, list[dict[str, float]]] = {"bids": [], "asks": []}
        down: dict[str, list[dict[str, float]]] = {"bids": [], "asks": []}
        if token_up:
            try:
                raw = await self.get_book(token_up)
                up = {
                    "bids": levels_from_book(raw, "bids"),
                    "asks": levels_from_book(raw, "asks"),
                }
            except Exception:
                pass
        if token_down:
            try:
                raw = await self.get_book(token_down)
                down = {
                    "bids": levels_from_book(raw, "bids"),
                    "asks": levels_from_book(raw, "asks"),
                }
            except Exception:
                pass
        return up, down
