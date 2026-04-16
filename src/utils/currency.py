"""
Конвертация валют для фильтрации по минимальному бюджету.
Использует актуальные курсы с открытых API.
"""

import os
from typing import Dict, Optional
from loguru import logger
import httpx
from datetime import datetime, timedelta


class CurrencyConverter:
    """Конвертер валют с кэшированием курсов."""

    # Кэш курсов на 1 час
    CACHE_TTL_HOURS = 1

    def __init__(self):
        self._rates: Dict[str, float] = {}
        self._base_currency: str = "RUB"
        self._last_update: Optional[datetime] = None

    async def _fetch_rates(self):
        """Получить курсы валют с API ЦБ РФ (или fallback на exchangerate-api)."""
        try:
            # Пробуем ЦБ РФ (самый точный для рублей)
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    "https://www.cbr-xml-daily.ru/daily_json.js",
                    timeout=10.0
                )
                if response.status_code == 200:
                    data = response.json()
                    rates = {"RUB": 1.0}
                    for code, info in data.get("Valute", {}).items():
                        rates[code] = info["Value"] / info["Nominal"]
                    # Добавляем USD и EUR из ЦБ
                    if "USD" in rates and "EUR" in rates:
                        self._rates = rates
                        self._last_update = datetime.now()
                        logger.info(f"Курсы валют обновлены: {len(rates)} валют")
                        return
        except Exception as e:
            logger.debug(f"Не удалось получить курсы ЦБ: {e}")

        # Fallback на exchangerate-api (не требует ключ для базового tier)
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"https://api.exchangerate-api.com/v4/latest/{self._base_currency}",
                    timeout=10.0
                )
                if response.status_code == 200:
                    data = response.json()
                    self._rates = data.get("rates", {})
                    self._rates[self._base_currency] = 1.0
                    self._last_update = datetime.now()
                    logger.info(f"Курсы валют обновлены (fallback): {len(self._rates)} валют")
                    return
        except Exception as e:
            logger.debug(f"Не удалось получить курсы exchangerate-api: {e}")

        # Если всё упало - используем фиксированные курсы
        self._rates = {
            "RUB": 1.0,
            "USD": 90.0,
            "EUR": 98.0,
            "GBP": 115.0,
            "JPY": 0.6,
            "CNY": 12.5,
            "KZT": 0.19,
            "BYN": 28.0,
            "UAH": 2.2,
        }
        self._last_update = datetime.now()
        logger.warning(f"Используем фиксированные курсы валют")

    async def ensure_rates(self):
        """Убедиться что курсы актуальны."""
        if not self._rates or not self._last_update:
            await self._fetch_rates()
            return

        age = datetime.now() - self._last_update
        if age > timedelta(hours=self.CACHE_TTL_HOURS):
            await self._fetch_rates()

    async def to_rub(self, amount: float, currency: str) -> float:
        """Конвертировать сумму в рубли."""
        await self.ensure_rates()

        currency = currency.upper().replace("₽", "RUB").replace("$", "USD").replace("€", "EUR")
        if currency in ["RUB", "RUR"]:
            return amount

        if currency in self._rates:
            rate = self._rates[currency]
            return amount * rate

        logger.warning(f"Неизвестная валюта: {currency}, возвращаем как есть")
        return amount

    def format_budget(self, amount: float, currency: str) -> str:
        """Форматировать бюджет для отображения."""
        symbols = {
            "RUB": "₽",
            "USD": "$",
            "EUR": "€",
            "GBP": "£",
            "JPY": "¥",
            "CNY": "¥",
        }
        symbol = symbols.get(currency.upper(), currency)
        if currency.upper() in ["RUB", "JPY", "CNY", "KZT", "BYN", "UAH"]:
            return f"{int(amount)} {symbol}"
        return f"{symbol}{amount:.0f}"


# Singleton
_converter: Optional[CurrencyConverter] = None


async def get_converter() -> CurrencyConverter:
    global _converter
    if _converter is None:
        _converter = CurrencyConverter()
    return _converter
