from __future__ import annotations

import logging
from typing import Any

from .service import DealsService

logger = logging.getLogger(__name__)


class DealsController:
    """Thin controller wrapping DealsService — matches TaskAutomationController pattern."""

    def __init__(self, service: DealsService) -> None:
        self.service = service

    async def search(self, *, query: str | None = None, url: str | None = None) -> dict[str, Any]:
        logger.info("DealsController: search query=%s url=%s", query, url)
        return await self.service.search(query=query, url=url)

    def get_history(self, product_id: str) -> list[dict[str, Any]]:
        logger.info("DealsController: history product_id=%s", product_id)
        return self.service.get_history(product_id)

    def set_alert(self, product_id: str, target_price: float) -> dict[str, Any]:
        logger.info("DealsController: alert product_id=%s target=%.2f", product_id, target_price)
        return self.service.set_alert(product_id, target_price)
