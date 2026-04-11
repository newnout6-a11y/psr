"""
RAG-пайплайн для сопоставления заказов с компетенциями разработчика.
Использует векторную БД (FAISS) для семантического поиска.
"""

import os
import json
from typing import Optional, Dict, Any, List
from pathlib import Path
from loguru import logger


class RAGPipeline:
    """
    Retrieval-Augmented Generation пайплайн.
    Сопоставляет заказы с портфолио и кейсами разработчика.
    """
    
    def __init__(
        self,
        portfolio_path: str = "data/portfolio.json",
        cases_path: str = "data/cases.json",
        embedding_model: str = "all-MiniLM-L6-v2",
        similarity_threshold: float = 0.75,
    ):
        self.portfolio_path = portfolio_path
        self.cases_path = cases_path
        self.embedding_model = embedding_model
        self.similarity_threshold = similarity_threshold
        
        # Векторная БД
        self.db = None
        self.embeddings = None
        self.cases = []
        
        # Загрузка данных
        self.portfolio = self._load_json(portfolio_path)
        self._load_cases(cases_path)
        
        # Инициализация embeddings (ленивая)
        self._initialized = False
    
    def initialize(self):
        """Ленивая инициализация ML-моделей."""
        if self._initialized:
            return
        
        try:
            from sentence_transformers import SentenceTransformer
            import faiss
            
            logger.info(f"Загрузка модели эмбеддингов: {self.embedding_model}")
            self.embeddings = SentenceTransformer(self.embedding_model)
            
            # Индекс FAISS
            if self.cases:
                case_texts = [c.get("title", "") + " " + c.get("description", "") for c in self.cases]
                case_embeddings = self.embeddings.encode(case_texts)
                
                dimension = case_embeddings.shape[1]
                self.db = faiss.IndexFlatL2(dimension)
                self.db.add(case_embeddings.astype("float32"))
                
                logger.info(f"FAISS индекс создан: {len(self.cases)} кейсов, {dimension} измерений")
            
            self._initialized = True
        
        except ImportError:
            logger.warning("sentence-transformers или faiss не установлены. RAG отключён.")
            return
    
    def match_project(self, project: Dict[str, Any]) -> Dict[str, Any]:
        """
        Сопоставление проекта с портфолио/кейсами.
        
        Returns:
            {
                "score": 0.0-1.0,
                "matched_cases": [...],
                "relevant_skills": [...],
                "recommendation": "apply|skip|review",
            }
        """
        self.initialize()
        
        title = project.get("title", "")
        description = project.get("description", "")
        skills = project.get("skills", [])
        
        # Эмбеддинг проекта
        project_text = f"{title} {description}"
        project_embedding = self.embeddings.encode([project_text]).astype("float32")
        
        # Поиск похожих кейсов
        matched_cases = []
        if self.db and self.cases:
            distances, indices = self.db.search(project_embedding, k=min(5, len(self.cases)))
            
            for dist, idx in zip(distances[0], indices[0]):
                if idx < len(self.cases):
                    # FAISS возвращает L2 расстояние → конвертируем в similarity
                    similarity = 1 / (1 + dist)
                    if similarity >= self.similarity_threshold:
                        matched_cases.append({
                            **self.cases[idx],
                            "similarity": float(similarity),
                        })
        
        # Пересечение навыков
        relevant_skills = self._find_relevant_skills(skills)
        
        # Общий скор
        case_score = max([c["similarity"] for c in matched_cases], default=0.0)
        skill_score = min(len(relevant_skills) / max(len(skills), 1), 1.0) if skills else 0.0
        
        total_score = case_score * 0.6 + skill_score * 0.4
        
        # Рекомендация
        if total_score >= 0.85:
            recommendation = "apply"
        elif total_score >= 0.6:
            recommendation = "review"
        else:
            recommendation = "skip"
        
        return {
            "score": float(total_score),
            "matched_cases": matched_cases,
            "relevant_skills": relevant_skills,
            "recommendation": recommendation,
        }
    
    def get_portfolio_context(self, project: Dict[str, Any]) -> str:
        """
        Получить контекст портфолио для генерации отклика.
        Включает релевантные кейсы и навыки.
        """
        match_result = self.match_project(project)
        
        context_parts = []
        
        # Информация о разработчике
        dev_info = self.portfolio.get("developer", {})
        if dev_info:
            context_parts.append(f"Имя: {dev_info.get('name', '')}")
            context_parts.append(f"Стек: {', '.join(dev_info.get('skills', []))}")
            context_parts.append(f"Опыт: {dev_info.get('experience_years', 0)} лет")
        
        # Релевантные кейсы
        if match_result["matched_cases"]:
            context_parts.append("\nРелевантные кейсы:")
            for i, case in enumerate(match_result["matched_cases"][:3], 1):
                context_parts.append(f"{i}. {case.get('title', '')}")
                context_parts.append(f"   Описание: {case.get('description', '')}")
                context_parts.append(f"   Стек: {', '.join(case.get('tech', []))}")
                context_parts.append(f"   Результат: {case.get('result', '')}")
        
        # Навыки
        if match_result["relevant_skills"]:
            context_parts.append(f"\nПодходящие навыки: {', '.join(match_result['relevant_skills'])}")
        
        return "\n".join(context_parts)
    
    def add_case(self, case: Dict[str, Any]):
        """Добавить новый кейс в базу."""
        self.cases.append(case)
        
        # Переиндексация
        if self._initialized and self.cases and self.embeddings:
            case_texts = [c.get("title", "") + " " + c.get("description", "") for c in self.cases]
            case_embeddings = self.embeddings.encode(case_texts)
            
            dimension = case_embeddings.shape[1]
            self.db = faiss.IndexFlatL2(dimension)
            self.db.add(case_embeddings.astype("float32"))
            
            logger.info(f"Кейс добавлен: {case.get('title', '')}")
    
    def save_cases(self, path: Optional[str] = None):
        """Сохранить кейсы в файл."""
        output_path = path or self.cases_path
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.cases, f, ensure_ascii=False, indent=2)
        logger.info(f"Кейсы сохранены: {output_path}")
    
    def _load_json(self, path: str) -> Dict:
        """Загрузка JSON-файла."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            logger.warning(f"Файл не найден: {path}")
            return {}
        except json.JSONDecodeError:
            logger.error(f"Ошибка парсинга JSON: {path}")
            return {}
    
    def _load_cases(self, path: str):
        """Загрузка кейсов."""
        data = self._load_json(path)
        if isinstance(data, list):
            self.cases = data
        elif isinstance(data, dict) and "cases" in data:
            self.cases = data["cases"]
        else:
            self.cases = []
        
        logger.info(f"Загружено {len(self.cases)} кейсов")
    
    def _find_relevant_skills(self, project_skills: List[str]) -> List[str]:
        """Найти релевантные навыки разработчика."""
        dev_skills = self.portfolio.get("developer", {}).get("skills", [])
        
        # Простое пересечение (можно улучшить через семантику)
        project_skills_lower = [s.lower() for s in project_skills]
        dev_skills_lower = [s.lower() for s in dev_skills]
        
        relevant = []
        for skill in project_skills_lower:
            for dev_skill in dev_skills_lower:
                if skill in dev_skill or dev_skill in skill:
                    relevant.append(dev_skill)
        
        return list(set(relevant))
