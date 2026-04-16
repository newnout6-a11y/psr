"""
Планировщик запусков системы фриланс-автоматизации.
Позволяет запускать только в определенные часы (например, рабочее время).
"""

import os
from datetime import datetime, time
from typing import List, Optional
import pytz
from loguru import logger


class ScheduleManager:
    """Управление расписанием работы системы."""

    def __init__(self):
        self.timezone = pytz.timezone(os.getenv("TIMEZONE_REGION", "Europe/Moscow"))
        
        # Рабочие часы (по умолчанию 9:00 - 18:00)
        work_hours = os.getenv("WORK_HOURS", "9-18")
        try:
            start, end = map(int, work_hours.split("-"))
            self.work_start = time(start, 0)
            self.work_end = time(end, 0)
        except ValueError:
            self.work_start = time(9, 0)
            self.work_end = time(18, 0)
        
        # Рабочие дни (по умолчанию пн-пт)
        work_days = os.getenv("WORK_DAYS", "1,2,3,4,5")  # 1=понедельник, 7=воскресенье
        try:
            self.work_days = [int(d.strip()) for d in work_days.split(",")]
        except ValueError:
            self.work_days = [1, 2, 3, 4, 5]

    def is_work_time(self) -> bool:
        """Проверить, что сейчас рабочее время."""
        now = datetime.now(self.timezone)
        
        # Проверка дня недели
        if now.isoweekday() not in self.work_days:
            return False
        
        # Проверка часа
        current_time = now.time()
        if self.work_start <= self.work_end:
            # Обычный случай (9:00 - 18:00)
            return self.work_start <= current_time < self.work_end
        else:
            # Ночная смена (20:00 - 02:00)
            return current_time >= self.work_start or current_time < self.work_end

    def get_next_run_delay(self) -> int:
        """Получить количество секунд до следующего рабочего периода."""
        now = datetime.now(self.timezone)
        
        if self.is_work_time():
            return 0
        
        # Ищем ближайший рабочий день и час
        days_checked = 0
        check_date = now.date()
        
        while days_checked < 7:
            check_day = check_date.isoweekday()
            
            if check_day in self.work_days:
                if check_date == now.date():
                    # Сегодня - проверяем время
                    if now.time() < self.work_start:
                        next_start = datetime.combine(check_date, self.work_start)
                        delay = (next_start - now.replace(tzinfo=None)).total_seconds()
                        return int(delay)
                else:
                    # Будущий день - берем начало рабочего дня
                    next_start = datetime.combine(check_date, self.work_start)
                    delay = (next_start - now.replace(tzinfo=None)).total_seconds()
                    return int(delay)
            
            check_date = check_date + __import__('datetime').timedelta(days=1)
            days_checked += 1
        
        return 3600  # fallback: 1 час

    def get_status(self) -> str:
        """Статус расписания для отображения."""
        now = datetime.now(self.timezone)
        days_names = ["", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        days_str = ", ".join([days_names[d] for d in self.work_days])
        
        if self.is_work_time():
            return f"🟢 Рабочее время ({days_str}, {self.work_start.strftime('%H:%M')}-{self.work_end.strftime('%H:%M')})"
        else:
            delay = self.get_next_run_delay()
            hours = delay // 3600
            mins = (delay % 3600) // 60
            return f"⏳ Вне рабочего времени. Следующий запуск через {hours}ч {mins}м"


# Singleton
_schedule_manager: Optional[ScheduleManager] = None


def get_schedule_manager() -> ScheduleManager:
    global _schedule_manager
    if _schedule_manager is None:
        _schedule_manager = ScheduleManager()
    return _schedule_manager
