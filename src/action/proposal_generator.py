"""
Генерация сопроводительных писем через LLM (Groq / Gemini).
"""

import os
import json
import re
import random
from loguru import logger
from typing import Dict, Any, Optional
from groq import Groq
import google.generativeai as genai
from .proposal_templates import generate_proposal as generate_template_proposal

class ProposalGenerator:
    """Генератор откликов с использованием LLM."""
    
    def __init__(self):
        self.groq_api_key = os.getenv("GROQ_API_KEY")
        self.google_api_key = os.getenv("GOOGLE_API_KEY")
        self.provider = os.getenv("LLM_PROVIDER", "groq").lower()
        self.fallback = os.getenv("LLM_FALLBACK", "google").lower()
        
        self.groq_client = None
        if self.groq_api_key:
            try:
                self.groq_client = Groq(api_key=self.groq_api_key)
            except Exception as e:
                logger.error(f"Failed to init Groq: {e}")
                
        if self.google_api_key:
            genai.configure(api_key=self.google_api_key)
            
        # Загружаем портфолио
        self.portfolio = self._load_portfolio()
        
        # Системный промпт для РУССКОГО (контроль перплексии, живости текста)
        self.system_prompt_ru = """
Ты — профессиональный разработчик на фрилансе. Напиши короткий и четкий отклик заказчику.
ПРАВИЛА (СТРОГО!):
1. ПРИВЕТСТВИЕ: Всегда начинай со слова "Здравствуйте!". Это единственно допустимое приветствие.
2. НИКАКИХ СМАЙЛИКОВ И ЭМОДЗИ. Текст должен быть профессиональным и чистым.
3. НИКАКОГО MARKDOWN И СПЕЦСИМВОЛОВ. Запрещено: *, #, _, -, списки, жирный шрифт. Только буквы и знаки препинания.
4. СТИЛЬ: Пиши как человек в мессенджере, но вежливо. Сразу к делу.
5. НИКАКОГО ВСТУПИТЕЛЬНОГО ТЕКСТА. Не пиши "Вот вариант", "Конечно". Сразу текст сообщения.
6. РЕЛЕВАНТНЫЙ ОПЫТ: Из своих навыков выбери один пример, который подходит под задачу. Расскажи о нем одним предложением.
7. ВОПРОС: Задай один технический вопрос по задаче заказчика.
8. ДЛИНА: 2-4 предложения. Лаконично.
"""
        
        # Системный промпт для АНГЛИЙСКОГО
        self.system_prompt_en = """
You are a freelancer developer. Write a proposal for an IT project.
RULES (STRICT!):
1. NO MARKDOWN. Do not use *, #, _, -, or lists. Only plain text.
2. NO ASSISTANT FLUFF. Do not say "Sure!", "Here is your proposal". Start directly with the message.
3. BE SHORT AND REAL. 3-5 sentences maximum.
4. ASK A QUESTION. Use one smart technical question to open the chat.
"""

    def _load_portfolio(self) -> Dict[str, Any]:
        """Загрузка портфолио для контекста."""
        path = "data/portfolio.json"
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"developer": {"experience_years": 5}, "cases": []}
        
    def _clean_llm_response(self, text: str) -> str:
        """Финальная очистка текста от артефактов ИИ."""
        if not text: return ""
        
        # Удаляем "ассистентские" вступления
        meta = [r"Вот ваш отклик.*", r"Конечно.*", r"Безусловно.*", r"Предлагаю.*", r"Здравствуйте! Вот.*"]
        for p in meta:
            text = re.sub(p, "", text, flags=re.IGNORECASE | re.DOTALL)

        # Удаляем Markdown
        for char in "*#_`>":
            text = text.replace(char, "")
            
        # Удаляем эмодзи (диапазоны unicode)
        text = re.sub(r'[^\x00-\x7F\u0400-\u04FF\s.,!?()\-:;]', '', text)
        
        # Удаляем любые упоминания цены/денег (предохранитель)
        price_patterns = [
            r"\d+[\s\d]*\s*(руб|рублей|тыс|тысяч|т\.р|р\.)",
            r"\b(цена|стоимость|бюджет|стоит)\b.*?\d+\s*(руб|р)?",
            r"(бюджет|стоимость):\s*\d+.*"
        ]
        for p in price_patterns:
            text = re.sub(p, "", text, flags=re.IGNORECASE)

        # Удаляем двойные пробелы и переносы
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _sanitize_text(self, text: str) -> str:
        """Очистка от анти-спам ловушек и трекеров (honeypots)."""
        if not text: return ""
        text = re.sub(r'[A-Za-z0-9+/]{15,}={0,2}', '', text)
        text = re.sub(r'\b[A-Z]{6,}\b', '', text)
        text = re.sub(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', '', text)
        return re.sub(r'\s+', ' ', text).strip()
        
    def _build_user_prompt(self, project: Any, lang: str = "ru") -> str:
        """Сборка контекста заказа с полным профилем разработчика."""
        proj_skills = ", ".join(project.skills) if project.skills else "указанные в описании"
        budget = f"{project.budget} {project.currency}" if project.budget else "обсуждается"
        
        dev = self.portfolio.get("developer", {})
        my_skills = ", ".join(dev.get("skills", []))
        exp_years = dev.get("experience_years", 4)
        highlights = self.portfolio.get("portfolio_highlights", [])
        highlights_text = "; ".join(highlights[:3]) if highlights else "парсинг, автоматизация, веб-разработка"
        
        title_safe = self._sanitize_text(project.title)
        desc_safe = self._sanitize_text(project.description)
        
        if lang == "en":
            return f"PROJECT: {title_safe}\nDESC: {desc_safe}\nMY SKILLS: {my_skills}\nWrite 3-4 sentences proposal."
        else:
            return f"ЗАКАЗ: {title_safe}\nОПИСАНИЕ: {desc_safe}\nМОИ НАВЫКИ: {my_skills}\nСТАЖ: {exp_years} лет\nНапиши ЖИВОЙ отклик заказчику (3-4 предложения) с техническим вопросом."

    async def generate(self, project: Any) -> str:
        """Основной метод генерации с fallback'ами."""
        lang = "en" if project.platform in ["remoteok", "freelancer_com", "peopleperhour", "upwork"] else "ru"
        system_prompt = self.system_prompt_en if lang == "en" else self.system_prompt_ru
        prompt = self._build_user_prompt(project, lang)
        
        with open(r"c:\psr\data\last_llm_prompt.txt", "w", encoding="utf-8") as f:
            f.write(prompt)
        
        res = None
        # Основной провайдер
        if self.provider == "groq" and self.groq_client:
            res = await self._generate_groq(prompt, system_prompt)
        elif self.provider == "google" and self.google_api_key:
            res = await self._generate_gemini(prompt, system_prompt)
            
        # Fallback провайдер
        if not res:
            if self.fallback == "groq" and self.groq_client:
                res = await self._generate_groq(prompt, system_prompt)
            elif self.fallback == "google" and self.google_api_key:
                res = await self._generate_gemini(prompt, system_prompt)
            
        if res:
            res = self._clean_llm_response(res)
            with open(r"c:\psr\data\last_llm_response.txt", "w", encoding="utf-8") as f:
                f.write(res)
            return res
            
        logger.warning(f"LLM недоступны, используем шаблоны для отклика на {project.title}")
        return generate_template_proposal({"title": project.title, "found_skills": project.skills, "budget": project.budget}, self.portfolio)
        
    async def _generate_groq(self, prompt: str, system_prompt: str) -> Optional[str]:
        try:
            model = "llama-3.3-70b-versatile"
            completion = self.groq_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.75,
                max_tokens=500,
            )
            return completion.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Groq API Error: {e}")
            return None

    async def _generate_gemini(self, prompt: str, system_prompt: str) -> Optional[str]:
        try:
            model = genai.GenerativeModel('gemini-2.0-flash', system_instruction=system_prompt)
            response = model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            logger.error(f"Gemini API Error: {e}")
            return None
