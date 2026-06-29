"""
Модуль NLP-фильтрации заказов.
Анализ Job Description, извлечение стека технологий, отсев спама.
"""

import re
from typing import Dict, Any, List
from loguru import logger


class NLPFilter:
    """
    Фильтрация заказов на базе NLP.
    Извлекает стек технологий, бюджет, категорию.
    """
    
    # Паттерны технологий
    TECH_STACK_PATTERNS = {
        "python": r'\bpython\b|django|flask|fastapi|celery|scrapy',
        "javascript": r'\bjavascript\b|node\.?js|express|react|vue|angular|next\.?js',
        "typescript": r'\btypescript\b|ts\b',
        "go": r'\bgolang\b|\bgo\b',
        "java": r'\bjava\b|spring|hibernate',
        "php": r'\bphp\b|laravel|symfony|wordpress',
        "ruby": r'\bruby\b|rails',
        "rust": r'\brust\b|actix|tokio',
        "csharp": r'\bc#|csharp|\.net|asp\.?net',
        "devops": r'\bdocker\b|kubernetes|k8s|terraform|ansible|jenkins|ci/cd',
        "database": r'\bpostgresql\b|mysql|mongodb|redis|sqlite|prisma',
        "frontend": r'\bhtml\b|\bcss\b|sass|tailwind|bootstrap|webpack',
        "mobile": r'\breact native\b|flutter|swift|kotlin|compose|ios|android',
        "ai_ml": r'\bmachine learning\b|deep learning|tensorflow|pytorch|transformers|llm',
        "blockchain": r'\bblockchain\b|web3|solidity|smart contract|defi',
    }
    
    # Паттерны спама/ловушек
    SPAM_PATTERNS = [
        r'click here',
        r'contact me on telegram',
        r'whatsapp',
        r'send money',
        r'bitcoin',
        r'crypto payment',
        r'пожалуйста свяжитесь',
        r'напишите в личку',
    ]
    
    def __init__(self, spam_threshold: float = 0.3, relevance_threshold: float = 0.15):
        self.spam_threshold = spam_threshold
        self.relevance_threshold = relevance_threshold
    
    def analyze_project(self, project: Dict[str, Any]) -> Dict[str, Any]:
        """
        Полный анализ проекта.
        
        Returns:
            {
                "tech_stack": [...],
                "spam_score": 0.0-1.0,
                "relevance_score": 0.0-1.0,
                "is_spam": bool,
                "is_relevant": bool,
                "budget_category": "low|medium|high",
                "complexity": "easy|medium|hard",
            }
        """
        text = f"{project.get('title', '')} {project.get('description', '')}".lower()
        
        # Извлечение стека технологий
        tech_stack = self._extract_tech_stack(text)
        
        # Оценка спама
        spam_score = self._calculate_spam_score(text)
        
        # Оценка релевантности
        relevance_score = self._calculate_relevance(text, tech_stack)
        
        # Категория бюджета
        budget_category = self._classify_budget(project.get("budget", 0))
        
        # Сложность
        complexity = self._estimate_complexity(text, tech_stack)
        
        result = {
            "tech_stack": tech_stack,
            "spam_score": spam_score,
            "relevance_score": relevance_score,
            "is_spam": spam_score > self.spam_threshold,
            "is_relevant": relevance_score > self.relevance_threshold,
            "budget_category": budget_category,
            "complexity": complexity,
        }
        
        logger.debug(
            f"Анализ проекта {project.get('id', 'unknown')}: "
            f"spam={spam_score:.2f}, relevance={relevance_score:.2f}, "
            f"stack={tech_stack}"
        )
        
        return result
    
    def extract_requirements(self, text: str) -> Dict[str, Any]:
        """
        Извлечение требований из описания.
        """
        requirements = {
            "technologies": self._extract_tech_stack(text),
            "experience_level": self._detect_experience_level(text),
            "deadline_mentions": self._extract_deadlines(text),
            "budget_mentions": self._extract_budget_mentions(text),
            "deliverables": self._extract_deliverables(text),
        }
        
        return requirements
    
    def is_honeypot(self, project: Dict[str, Any]) -> bool:
        """
        Проверка на генеративную ловушку (honeypot).
        Анализирует семантическую логичность, ищет скрытые теги.
        """
        description = project.get("description", "")

        # Проверка на скрытые элементы
        honeypot_indicators = [
            '<img src="pixel',  # Невидимые пиксели
            'display:none',
            'visibility:hidden',
            'font-size:0',
            'color:#fff',  # Белый текст на белом фоне
        ]
        
        html_honeypots = sum(1 for indicator in honeypot_indicators if indicator.lower() in description.lower())
        
        # Проверка на бессмысленный контент
        words = description.split()
        unique_ratio = len(set(words)) / len(words) if words else 0
        
        # Слишком низкая уникальность слов → сгенерированный текст
        if unique_ratio < 0.2 and len(words) > 50:
            return True
        
        # Много скрытых элементов
        if html_honeypots > 1:
            return True
        
        return False
    
    def _extract_tech_stack(self, text: str) -> List[str]:
        """Извлечение упомянутых технологий."""
        found = []
        for tech, pattern in self.TECH_STACK_PATTERNS.items():
            if re.search(pattern, text, re.IGNORECASE):
                found.append(tech)
        return found
    
    def _calculate_spam_score(self, text: str) -> float:
        """Расчёт вероятности спама (0.0-1.0)."""
        spam_hits = 0
        for pattern in self.SPAM_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                spam_hits += 1
        
        # Нормализация
        score = min(spam_hits / len(self.SPAM_PATTERNS), 1.0)
        return score
    
    def _calculate_relevance(self, text: str, tech_stack: List[str]) -> float:
        """Расчёт релевантности заказа (0.0-1.0)."""
        if not tech_stack:
            return 0.1
        
        # Базовая релевантность по количеству технологий
        tech_score = min(len(tech_stack) / 5, 1.0)
        
        # Дополнительный вес за описание деталей
        detail_indicators = [
            "api", "database", "server", "deployment",
            "требования", "задача", "необходимо", "нужно",
        ]
        detail_hits = sum(1 for word in detail_indicators if word in text.lower())
        detail_score = min(detail_hits / len(detail_indicators), 1.0)
        
        # Взвешенная средняя
        return tech_score * 0.6 + detail_score * 0.4
    
    def _classify_budget(self, budget) -> str:
        """Классификация бюджета."""
        if budget is None or budget == 0:
            return "unknown"
        if budget < 5000:
            return "low"
        elif budget < 50000:
            return "medium"
        else:
            return "high"
    
    def _estimate_complexity(self, text: str, tech_stack: List[str]) -> str:
        """Оценка сложности задачи."""
        complexity_score = len(tech_stack)
        
        # Дополнительные индикаторы сложности
        complex_keywords = ["architecture", "microservices", "scale", "optimize",
                          "архитектура", "масштабирование", "оптимизация"]
        for keyword in complex_keywords:
            if keyword in text.lower():
                complexity_score += 1
        
        if complexity_score <= 2:
            return "easy"
        elif complexity_score <= 5:
            return "medium"
        else:
            return "hard"
    
    def _detect_experience_level(self, text: str) -> str:
        """Определение требуемого уровня опыта."""
        junior_patterns = [r'junior', r'начинающий', r'простой']
        senior_patterns = [r'senior', r'lead', r'архитектор', r'эксперт']
        
        for pattern in senior_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return "senior"
        
        for pattern in junior_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return "junior"
        
        return "middle"
    
    def _extract_deadlines(self, text: str) -> List[str]:
        """Извлечение упоминаний дедлайнов."""
        deadline_patterns = [
            r'\d+\s*(дня|дней|недел|месяц)',
            r'by\s+\w+\s+\d+',
            r'до\s+\d+\s+\w+',
            r'deadline[:\s]+\S+',
        ]
        
        deadlines = []
        for pattern in deadline_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            deadlines.extend(matches)
        
        return deadlines
    
    def _extract_budget_mentions(self, text: str) -> List[str]:
        """Извлечение упоминаний бюджета."""
        budget_patterns = [
            r'\$\s*\d[\d,]*',
            r'\d+\s*(руб|usd|eur|₽|\$)',
            r'бюджет[:\s]+\S+',
            r'budget[:\s]+\S+',
        ]
        
        mentions = []
        for pattern in budget_patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            mentions.extend(matches)
        
        return mentions
    
    def _extract_deliverables(self, text: str) -> List[str]:
        """Извлечение ожидаемых результатов."""
        deliverable_keywords = [
            "api", "сайт", "приложение", "скрипт", "парсер",
            "бот", "плагин", "модуль", "интеграция", "dashboard",
            "website", "app", "script", "plugin", "extension",
        ]
        
        found = []
        for keyword in deliverable_keywords:
            if keyword in text.lower():
                found.append(keyword)
        
        return found
