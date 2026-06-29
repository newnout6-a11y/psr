"""Project filtering before NLP/AI scoring."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List

import yaml
from loguru import logger

from src.action.proposal_db import ProposalDB
from src.parsers.base_parser import ProjectItem
from src.utils.currency import get_converter
from src.utils.time_utils import parse_created_at_datetime


@dataclass
class FilterDecision:
    project: ProjectItem
    passed: bool
    reason: str = "passed"
    matched_skills: list[str] | None = None


class ProjectFilter:
    def __init__(self, config_path: str = "config/filters.yaml"):
        self.config_path = config_path
        self._load_config()
        self.db = ProposalDB()
        self._converter = None

    async def _get_converter(self):
        if self._converter is None:
            self._converter = await get_converter()
        return self._converter

    def _project_age_hours(self, project: ProjectItem) -> float | None:
        created_dt = parse_created_at_datetime(project.created_at, project.platform)
        if created_dt is None:
            return None

        if created_dt.tzinfo is not None:
            age_seconds = (datetime.now(timezone.utc) - created_dt.astimezone(timezone.utc)).total_seconds()
        else:
            age_seconds = (datetime.now() - created_dt).total_seconds()

        return max(age_seconds, 0) / 3600

    def _load_config(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}

            self.required_skills = [s.lower() for s in config.get("required_skills", [])]
            self.stop_words = [s.lower() for s in config.get("stop_words", [])]
            self.min_budget = config.get("min_budget", 0)
            self.max_budget = config.get("max_budget", 0)
            self.per_page = config.get("per_page", 20)
            self.max_age_hours = config.get("max_age_hours", 48)
            self.max_proposals = config.get("max_proposals", 0)
            logger.info(
                f"Filters loaded: budget {self.min_budget}-{self.max_budget or 'inf'} RUB, "
                f"age <= {self.max_age_hours}h, per_page={self.per_page}"
            )
        except Exception as e:
            logger.error(f"Filter config load failed: {e}")
            self.required_skills = []
            self.stop_words = []
            self.min_budget = 0
            self.max_budget = 0
            self.per_page = 20
            self.max_age_hours = 0
            self.max_proposals = 0

    async def filter_projects_with_reasons(
        self,
        projects: List[ProjectItem],
    ) -> tuple[List[ProjectItem], dict[tuple[str, str], FilterDecision]]:
        filtered: list[ProjectItem] = []
        decisions: dict[tuple[str, str], FilterDecision] = {}
        reject_counts: Counter[str] = Counter()
        reject_examples: list[FilterDecision] = []
        seen_urls = set()

        sent_proposals = self.db.get_sent_proposals(limit=1000)
        sent_urls = {p.get("url") for p in sent_proposals if p.get("url")}

        converter = await self._get_converter()

        def key(project: ProjectItem) -> tuple[str, str]:
            return project.platform, project.id

        def reject(project: ProjectItem, reason: str) -> None:
            decision = FilterDecision(project=project, passed=False, reason=reason)
            decisions[key(project)] = decision
            reject_counts[reason.split(":", 1)[0]] += 1
            if len(reject_examples) < 12:
                reject_examples.append(decision)

        for project in projects:
            if getattr(project, "url", None) in seen_urls:
                reject(project, "duplicate_url")
                continue
            if getattr(project, "url", None) in sent_urls:
                reject(project, "already_sent_or_draft")
                continue

            seen_urls.add(getattr(project, "url", None))

            if self.max_proposals > 0 and (project.offers_count or 0) > self.max_proposals:
                reject(project, f"too_many_offers:{project.offers_count}>{self.max_proposals}")
                continue

            if self.max_age_hours > 0:
                age_hours = self._project_age_hours(project)
                if age_hours is not None and age_hours > self.max_age_hours:
                    reject(project, f"too_old:{age_hours:.1f}h>{self.max_age_hours}h")
                    continue

            text = f"{project.title} {project.description}".lower()
            skills_text = " ".join([skill.lower() for skill in project.skills])
            full_text = f"{text} {skills_text}"

            found_skills = [required for required in self.required_skills if required in full_text]

            if not found_skills:
                stop_hits = [word for word in self.stop_words if word in full_text]
                if stop_hits:
                    reject(project, f"stop_word:{', '.join(stop_hits[:3])}")
                else:
                    reject(project, "missing_required_skills")
                continue

            stop_hits = [word for word in self.stop_words if word in full_text]
            if stop_hits:
                logger.debug(
                    f"KeywordFilter: soft stop-word match for '{project.title[:50]}' "
                    f"skills={found_skills[:4]} stop={stop_hits[:3]}"
                )

            if project.budget is not None:
                budget_rub = await converter.to_rub(project.budget, project.currency)
                if self.min_budget > 0 and budget_rub < self.min_budget:
                    reject(project, f"budget_low:{budget_rub:.0f}<{self.min_budget}")
                    continue
                if self.max_budget > 0 and budget_rub > self.max_budget:
                    reject(project, f"budget_high:{budget_rub:.0f}>{self.max_budget}")
                    continue

            project.skills = list(set(project.skills + found_skills))
            filtered.append(project)
            decisions[key(project)] = FilterDecision(
                project=project,
                passed=True,
                matched_skills=found_skills,
            )

        logger.info(f"KeywordFilter: passed={len(filtered)}/{len(projects)} rejected={dict(reject_counts)}")
        for decision in reject_examples:
            project = decision.project
            logger.info(
                f"KeywordFilter reject: id={project.id} query='{project.search_query}' "
                f"title='{project.title[:70]}' reason={decision.reason}"
            )

        return filtered, decisions

    async def filter_projects(self, projects: List[ProjectItem]) -> List[ProjectItem]:
        filtered, _decisions = await self.filter_projects_with_reasons(projects)
        return filtered
