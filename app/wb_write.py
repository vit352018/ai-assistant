from .wb import WB


class WBWriter(WB):
    """Клиент с токеном на запись. Пользуется им только исполнитель подтверждённых действий (бот по кнопке ✅),
    инструменты, которые вызывает модель, до него не добираются."""

    async def update_stocks(self, warehouse_id, stocks):
        """PUT остатков по chrtId (старое поле sku с 20.05.2026 WB не принимает)."""
        if not 0 < len(stocks) <= 1000:
            raise ValueError("за один запрос можно обновить от 1 до 1000 размеров")
        return await self._req("PUT", "marketplace", f"/api/v3/stocks/{int(warehouse_id)}", body={"stocks": stocks})
