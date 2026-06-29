"""
RAG-пайплайн для сопоставления заказов с компетенциями разработчика.

Использует FAISS, но умеет деградировать до keyword-only режима.
Генерирует компактный контекст из 1-2 релевантных кейсов, нормализованных навыков
и типа проекта, чтобы отклик был конкретным, а не общим.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

import numpy as np
from loguru import logger

from src.paths import CASES_FILE, PORTFOLIO_FILE


class RAGPipeline:
    """Retrieval-Augmented Generation пайплайн."""

    _SKILL_ALIASES = {
        "python": {"python", "fastapi", "flask", "django", "celery"},
        "telegram": {"telegram", "aiogram", "telethon", "бот", "bot"},
        "parsing": {"parser", "парсер", "scraping", "scraper", "crawl", "скрейп"},
        "automation": {"automation", "автоматизация", "script", "скрипт", "integration", "интеграция"},
        "api": {"api", "rest", "backend", "webhook", "бекенд"},
        "frontend": {"react", "frontend", "javascript", "typescript", "html", "css", "vue"},
        "data": {"sql", "postgres", "mysql", "redis", "etl", "pandas"},
    }

    def __init__(
        self,
        portfolio_path: str = str(PORTFOLIO_FILE),
        cases_path: str = str(CASES_FILE),
        embedding_model: str = "all-MiniLM-L6-v2",
        similarity_threshold: float = 0.72,
    ):
        self.portfolio_path = portfolio_path
        self.cases_path = cases_path
        self.embedding_model = embedding_model
        self.similarity_threshold = similarity_threshold

        self.db = None
        self.embeddings = None
        self.cases: list[dict[str, Any]] = []
        self.portfolio = self._load_json(portfolio_path)
        self._load_cases(cases_path)
        self._initialized = False
        self._case_id_to_index: dict[str, int] = {}

    def initialize(self) -> None:
        if self._initialized:
            return

        try:
            import faiss
            from sentence_transformers import SentenceTransformer

            self.embeddings = SentenceTransformer(self.embedding_model)
            index_path = self.cases_path.replace(".json", ".faiss")
            mapping_path = self.cases_path.replace(".json", ".mapping")

            if os.path.exists(index_path) and os.path.exists(mapping_path):
                self.db = faiss.read_index(index_path)
                with open(mapping_path, "r", encoding="utf-8") as f:
                    self._case_id_to_index = json.load(f)
                logger.info(f"RAG: индекс загружен, кейсов={len(self._case_id_to_index)}")
            elif self.cases:
                texts = [self._case_text(case) for case in self.cases]
                vectors = self.embeddings.encode(texts)
                dimension = vectors.shape[1]
                base_index = faiss.IndexFlatL2(dimension)
                self.db = faiss.IndexIDMap(base_index)
                ids = np.arange(len(self.cases), dtype=np.int64)
                self.db.add_with_ids(vectors.astype("float32"), ids)
                self._case_id_to_index = {str(i): i for i in range(len(self.cases))}
                self._save_index()
                logger.info(f"RAG: индекс построен, кейсов={len(self.cases)}, dim={dimension}")
            else:
                logger.info("RAG: кейсов нет, используем keyword-only режим")
            self._initialized = True
        except ImportError:
            logger.warning("RAG: sentence-transformers/faiss не установлены, keyword-only режим")
        except Exception as e:
            logger.warning(f"RAG: инициализация не удалась, keyword-only режим: {e}")

    def analyze_project(self, project: dict[str, Any]) -> dict[str, Any]:
        self.initialize()

        title = str(project.get("title") or "")
        description = str(project.get("description") or "")
        skills = list(project.get("skills") or [])
        text = f"{title} {description}".strip()

        matched_cases = self._search_cases(text)
        normalized_skills = self._find_relevant_skills(skills, text)
        project_type = self._infer_project_type(text.lower())

        case_score = max((case["similarity"] for case in matched_cases), default=0.0)
        skill_score = min(len(normalized_skills) / 4, 1.0)
        score = round(case_score * 0.65 + skill_score * 0.35, 3)

        if score >= 0.78:
            recommendation = "apply"
        elif score >= 0.52:
            recommendation = "review"
        else:
            recommendation = "skip"

        return {
            "score": float(score),
            "recommendation": recommendation,
            "matched_cases": matched_cases[:2],
            "relevant_skills": normalized_skills,
            "project_type": project_type,
        }

    def match_project(self, project: dict[str, Any]) -> dict[str, Any]:
        return self.analyze_project(project)

    def get_portfolio_context(self, project: dict[str, Any]) -> str:
        analysis = self.analyze_project(project)
        developer = self.portfolio.get("developer", {})
        portfolio_skills = ", ".join(developer.get("skills", []))
        experience = developer.get("experience_years", 0)
        lines = [
            f"Профиль: {portfolio_skills}",
            f"Опыт: {experience} лет",
            f"Тип проекта: {analysis['project_type']}",
        ]

        if analysis["relevant_skills"]:
            lines.append(f"Нормализованные подходящие навыки: {', '.join(analysis['relevant_skills'])}")

        if analysis["matched_cases"]:
            lines.append("Релевантные кейсы:")
            for index, case in enumerate(analysis["matched_cases"], 1):
                tech = ", ".join(case.get("tech", [])[:5])
                result = case.get("result", "")
                lines.append(f"{index}. {case.get('title', '')} | стек: {tech} | результат: {result}")
                description = str(case.get("description") or "").strip()
                if description:
                    lines.append(f"   {description[:180]}")

        return "\n".join(lines)

    def add_case(self, case: dict[str, Any]) -> None:
        self.initialize()

        new_id = len(self.cases)
        case["_index_id"] = new_id
        self.cases.append(case)

        if self._initialized and self.db is not None and self.embeddings:
            text = self._case_text(case)
            vector = self.embeddings.encode([text]).astype("float32")
            id_array = np.array([new_id], dtype=np.int64)
            self.db.add_with_ids(vector, id_array)
            self._case_id_to_index[str(new_id)] = new_id
            self._save_index()
            logger.info(f"RAG: кейс добавлен инкрементально: {case.get('title', '')}")

    def save_cases(self, path: Optional[str] = None) -> None:
        output_path = path or self.cases_path
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.cases, f, ensure_ascii=False, indent=2)
        logger.info(f"RAG: кейсы сохранены: {output_path}")

    def _search_cases(self, project_text: str) -> list[dict[str, Any]]:
        if self.db is None or self.embeddings is None or not self.cases:
            return self._keyword_search_cases(project_text)

        try:
            vector = self.embeddings.encode([project_text]).astype("float32")
            distances, indices = self.db.search(vector, k=min(4, len(self.cases)))
        except Exception as e:
            logger.debug(f"RAG: vector search failed, fallback to keyword: {e}")
            return self._keyword_search_cases(project_text)

        out: list[dict[str, Any]] = []
        for dist, idx in zip(distances[0], indices[0], strict=False):
            if idx < 0 or idx >= len(self.cases):
                continue
            similarity = float(1 / (1 + dist))
            if similarity < self.similarity_threshold:
                continue
            case = dict(self.cases[idx])
            case["similarity"] = similarity
            out.append(case)

        if out:
            return sorted(out, key=lambda item: item["similarity"], reverse=True)
        return self._keyword_search_cases(project_text)

    def _keyword_search_cases(self, project_text: str) -> list[dict[str, Any]]:
        text = project_text.lower()
        ranked: list[dict[str, Any]] = []
        project_terms = self._term_set(text)

        for case in self.cases:
            case_text = self._case_text(case).lower()
            case_terms = self._term_set(case_text)
            overlap = len(project_terms & case_terms)
            tech_overlap = len(
                set(self._find_relevant_skills([], text))
                & set(self._find_relevant_skills(case.get("tech", []), case_text))
            )
            score = overlap + tech_overlap * 3
            if score <= 0:
                continue
            ranked.append({**case, "similarity": min(0.95, 0.35 + score * 0.06)})

        ranked.sort(key=lambda item: item["similarity"], reverse=True)
        return ranked[:2]

    def _term_set(self, text: str) -> set[str]:
        return {token for token in re.split(r"[^0-9a-zа-яё+#]+", text.lower()) if len(token) >= 3}

    def _case_text(self, case: dict[str, Any]) -> str:
        tech = " ".join(case.get("tech", []))
        return f"{case.get('title', '')} {case.get('description', '')} {tech} {case.get('result', '')}".strip()

    def _save_index(self) -> None:
        try:
            import faiss

            index_path = self.cases_path.replace(".json", ".faiss")
            mapping_path = self.cases_path.replace(".json", ".mapping")
            faiss.write_index(self.db, index_path)
            with open(mapping_path, "w", encoding="utf-8") as f:
                json.dump(self._case_id_to_index, f)
        except Exception as e:
            logger.debug(f"RAG: save index skipped: {e}")

    def _load_json(self, path: str) -> Any:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            logger.warning(f"RAG: файл не найден: {path}")
            return {}
        except json.JSONDecodeError:
            logger.error(f"RAG: битый JSON: {path}")
            return {}

    def _load_cases(self, path: str) -> None:
        data = self._load_json(path)
        if isinstance(data, list):
            self.cases = data
        elif isinstance(data, dict) and "cases" in data:
            self.cases = data["cases"]
        else:
            self.cases = []
        logger.info(f"RAG: кейсов загружено {len(self.cases)}")

    def _find_relevant_skills(self, project_skills: list[str], text: str = "") -> list[str]:
        developer_skills = self.portfolio.get("developer", {}).get("skills", [])
        skill_source = " ".join(project_skills or []) + " " + text
        source_text = skill_source.lower()

        matched_aliases = []
        for alias, terms in self._SKILL_ALIASES.items():
            if any(term in source_text for term in terms):
                matched_aliases.append(alias)

        if not matched_aliases:
            project_tokens = source_text.split()
            normalized = []
            for developer_skill in developer_skills:
                skill = str(developer_skill).lower()
                if any(skill in token or token in skill for token in project_tokens if len(token) >= 4):
                    normalized.append(skill)
            return sorted(set(normalized))

        return sorted(set(matched_aliases))

    def _infer_project_type(self, text: str) -> str:
        if any(term in text for term in {"telegram", "бот", "bot"}):
            return "bot"
        if any(term in text for term in {"parser", "парсер", "scraping", "скрейп"}):
            return "parser"
        if any(term in text for term in {"api", "rest", "backend", "webhook"}):
            return "api"
        if any(term in text for term in {"automation", "автоматизация", "script", "скрипт"}):
            return "automation"
        if any(term in text for term in {"frontend", "react", "website", "сайт"}):
            return "website"
        return "general"
