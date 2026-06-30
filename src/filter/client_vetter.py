"""
Веттинг заказчиков перед отправкой отклика.
Анализирует профиль заказчика: рейтинг, историю найма, отзывы.
Пропускает 'призраков' которые никогда не нанимают — экономит отклики.
"""

import re
from typing import Optional, Dict, Any  # Any used for OSINTResult type hint
from loguru import logger
from src.parsers.base_parser import ProjectItem
from src.platforms.kwork import get_kwork_service


class ClientVetter:
    """Оценка качества заказчика перед отправкой отклика."""

    # Весовые коэффициенты для итогового скора (0-100)
    WEIGHT_RATING = 30
    WEIGHT_HIRED = 30
    WEIGHT_REVIEWS = 20
    WEIGHT_DESCRIPTION = 20

    def __init__(self, config_path: str = "config/filters.yaml"):
        self.min_client_score = 30  # Минимальный скор заказчика (0-100)
        self.kwork_service = get_kwork_service()
        self._load_config(config_path)

    def set_kwork_api(self, api):
        """Установить Kwork API клиент (переиспользование из парсера)."""
        self.kwork_service._api = api

    def _load_config(self, config_path: str):
        try:
            import yaml

            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
            self.min_client_score = int(config.get("min_client_score", 30))
        except Exception as e:
            logger.debug(f"ClientVetter: не удалось загрузить конфиг: {e}")

    async def vet_client(
        self,
        project: ProjectItem,
        client_data: Optional[Dict[str, Any]] = None,
        osint_result: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Оценка заказчика. Возвращает dict с score и причинами.

        - `client_data`    — профиль из API (Kwork get_user).
        - `osint_result`   — OSINTResult из src.osint.OSINTAggregator (опционально).
        """
        score = 50  # Нейтральный по умолчанию
        reasons = []
        red_flags = []

        # 1. Анализ по данным заказчика (если есть — из API)
        if client_data:
            score, reasons, red_flags = self._vet_from_api(client_data, score, reasons, red_flags)

        # 2. Анализ по описанию проекта (всегда доступно)
        score, reasons, red_flags = self._vet_from_description(project, score, reasons, red_flags)

        # 3. OSINT-сигналы (GitHub, Habr, открытые упоминания, отзывы)
        if osint_result is not None:
            score, reasons, red_flags = self._vet_from_osint(osint_result, score, reasons, red_flags)

        passed = score >= self.min_client_score

        result = {
            "score": score,
            "passed": passed,
            "reasons": reasons,
            "red_flags": red_flags,
        }

        if not passed:
            logger.info(
                f"ClientVetter: заказчик НЕ прошёл веттинг ({score}/100) "
                f"для проекта '{project.title[:40]}': {red_flags}"
            )
        else:
            logger.debug(f"ClientVetter: заказчик прошёл ({score}/100) для '{project.title[:40]}'")

        return result

    def _vet_from_api(self, data: Dict, score: int, reasons: list, red_flags: list) -> tuple:
        """Анализ данных заказчика из API (рейтинг, отзывы, история)."""

        # Рейтинг
        rating = data.get("rating") or data.get("user_rating") or 0
        if isinstance(rating, (int, float)):
            if rating >= 4.5:
                score += 15
                reasons.append(f"высокий рейтинг {rating}")
            elif rating >= 3.5:
                score += 5
            elif rating > 0:
                score -= 15
                red_flags.append(f"низкий рейтинг {rating}")

        # Завершённые заказы (ключевой показатель!)
        completed = data.get("completed_orders_count", 0)
        if isinstance(completed, int):
            if completed >= 10:
                score += 20
                reasons.append(f"проверенный заказчик ({completed} завершённых заказов)")
            elif completed >= 3:
                score += 10
                reasons.append(f"есть опыт найма ({completed} заказов)")
            elif completed == 0:
                score -= 20
                red_flags.append("0 завершённых заказов — возможно 'призрак'")

        wants_count = data.get("wants_count")
        try:
            wants_count_int = int(wants_count) if wants_count not in (None, "") else 0
            if wants_count_int >= 3:
                score += 5
                reasons.append(f"публикует проекты регулярно ({wants_count_int})")
        except Exception:
            pass

        # % выполнения заказов
        done_pct = data.get("order_done_persent", 0)
        if isinstance(done_pct, (int, float)) and done_pct > 0:
            if done_pct >= 80:
                score += 10
                reasons.append(f"надёжный ({done_pct}% заказов завершено)")
            elif done_pct < 50 and completed > 0:
                score -= 10
                red_flags.append(f"низкий % завершения ({done_pct}%)")

        # % повторных заказов (лояльность)
        repeat_pct = data.get("order_done_repeat_persent", 0)
        if isinstance(repeat_pct, (int, float)) and repeat_pct > 0:
            score += 5
            reasons.append(f"возвращается ({repeat_pct}% повторных)")

        # Положительные vs отрицательные отзывы
        good = data.get("good_reviews", 0)
        bad = data.get("bad_reviews", 0)
        if isinstance(good, int) and isinstance(bad, int):
            total = good + bad
            if total >= 5:
                if good > bad * 2:
                    score += 10
                    reasons.append(f"хорошие отзывы ({good}/{total})")
                elif bad > good:
                    score -= 15
                    red_flags.append(f"плохие отзывы ({bad}/{total})")
            elif total == 0 and completed > 0:
                score -= 5
                red_flags.append("заказывает, но не оставляет отзывы")

        # Достижения (бейджи) — показатель активности
        achievments = data.get("achievments_count", 0)
        if isinstance(achievments, int) and achievments > 0:
            score += 5
            reasons.append(f"есть достижения ({achievments})")

        badges = data.get("badges")
        if isinstance(badges, list) and badges:
            score += 5
            reasons.append(f"Kwork badges: {len(badges)}")

        # Онлайн прямо сейчас
        if data.get("online"):
            score += 5
            reasons.append("онлайн сейчас — быстрый ответ")

        # Заблокировал ли нас
        if data.get("blocked_by_user"):
            score -= 30
            red_flags.append("заказчик заблокировал нас")

        # Дата регистрации (timestamp)
        reg_date = data.get("reg_date") or data.get("created_at") or ""
        if reg_date:
            try:
                import time

                reg_ts = int(reg_date) if str(reg_date).isdigit() else 0
                if reg_ts > 0:
                    days_old = (int(time.time()) - reg_ts) // 86400
                    if days_old < 30:
                        score -= 10
                        red_flags.append(f"новый аккаунт ({days_old} дней)")
                    elif days_old > 365:
                        score += 5
                        reasons.append(f"старый аккаунт ({days_old // 30} мес.)")
            except Exception:
                pass

        return score, reasons, red_flags

    def _vet_from_description(self, project: ProjectItem, score: int, reasons: list, red_flags: list) -> tuple:
        """Анализ качества описания проекта — косвенный индикатор заказчика."""

        desc = (project.description or "").strip()

        # Длина описания
        if len(desc) < 30:
            score -= 15
            red_flags.append("описание слишком короткое — заказчик не серьёзный")
        elif len(desc) > 100:
            score += 5
            reasons.append("подробное описание — серьёзный заказчик")

        # Конкретика в описании
        specificity_markers = [
            r"\b(api|rest|http|endpoint|база данных|бд|sql)\b",
            r"\b(telegram|бот|bot|вебхук|webhook)\b",
            r"\b(парсер|скрейпинг|scraping|crawl)\b",
            r"\b(автоматиз|интеграц|deploy|docker)\b",
            r"\b(срок|дедлайн|deadline|бюджет|сроки)\b",
        ]
        specific_count = sum(1 for pattern in specificity_markers if re.search(pattern, desc.lower()))
        if specific_count >= 2:
            score += 10
            reasons.append("конкретное ТЗ — заказчик знает что хочет")
        elif specific_count == 0:
            score -= 5
            red_flags.append("размытое описание — может не наймёт")

        # Красные флаги в описании
        scam_markers = [
            r"напишите в (телеграм|telegram|whatsapp|ватсап)",
            r"свяжитесь со мной лично",
            r"оплата после.*выполнения.*без.*гарантии",
            r"бесплатно.*тестовое",
        ]
        for pattern in scam_markers:
            if re.search(pattern, desc.lower()):
                score -= 20
                red_flags.append(f"подозрительный маркер: {pattern}")

        # Бюджет указан
        if project.budget and project.budget > 0:
            score += 5
            reasons.append(f"бюджет указан: {project.budget} {project.currency}")

        # Конкуренция — много откликов снижает шансы
        offers = project.offers_count
        if offers > 50:
            score -= 20
            red_flags.append(f"высокая конкуренция: {offers} откликов")
        elif offers > 20:
            score -= 10
            red_flags.append(f"средняя конкуренция: {offers} откликов")
        elif offers > 0:
            reasons.append(f"конкуренция низкая: {offers} откликов")
        else:
            score += 10
            reasons.append("нет откликов — можно быть первым")

        # % найма заказчика
        hired_pct = project.client_hired_percent
        if hired_pct and hired_pct >= 50:
            score += 10
            reasons.append(f"заказчик часто нанимает ({hired_pct}%)")
        elif hired_pct and hired_pct > 0:
            score += 5
        elif offers > 5 and hired_pct is None:
            score -= 10
            red_flags.append("заказчик никогда не нанимал, но уже много откликов")

        return min(100, max(0, score)), reasons, red_flags

    def _vet_from_osint(self, osint_result: Any, score: int, reasons: list, red_flags: list) -> tuple:
        """Инъекция OSINT-сигналов в веттинг.

        Смешиваем внутреннюю репутацию (0-100) с нашим скором 50/50,
        добавляем find-level red_flags / positive_signals.
        """
        try:
            osint_score = int(getattr(osint_result, "reputation_score", 50))
            # Мягкое смешивание: 70% наш скор, 30% OSINT
            score = int(score * 0.7 + osint_score * 0.3)

            pos = getattr(osint_result, "positive_signals", []) or []
            red = getattr(osint_result, "red_flags", []) or []
            reasons.extend(f"OSINT: {s}" for s in pos[:3])
            red_flags.extend(f"OSINT: {s}" for s in red[:3])
        except Exception as e:
            logger.debug(f"ClientVetter: ошибка интеграции OSINT: {e}")
        return min(100, max(0, score)), reasons, red_flags

    async def fetch_kwork_client_data(self, project_id: str, user_id: Optional[str] = None) -> Optional[Dict]:
        """Получить данные заказчика из общего KworkService."""
        return await self.kwork_service.fetch_client_data(project_id, user_id=user_id)
