"""Guarded proposal image generation.

This module is intentionally conservative: it can create and validate a draft
image artifact, but it only marks it attachable when the Kwork upload flow is
explicitly confirmed by config.
"""

from __future__ import annotations

import base64
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from src.brain.llm_router import OpenAICompatibleClient
from src.parsers.base_parser import ProjectItem
from src.paths import PROPOSAL_ASSETS_DIR, ensure_parent


def _env_bool(key: str, default: bool = False) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except Exception:
        return default


def _first_openai_key() -> str:
    keys = [os.getenv("OPENAI_API_KEY", "").strip()]
    raw_keys = os.getenv("OPENAI_API_KEYS", "")
    for delimiter in [";", "\n", "\r", "\t"]:
        raw_keys = raw_keys.replace(delimiter, ",")
    keys.extend(item.strip() for item in raw_keys.split(",") if item.strip())
    return next((key for key in keys if key), "")


def _openai_url(endpoint: str) -> str:
    base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com").rstrip("/")
    prefix = os.getenv("OPENAI_API_PREFIX", "/v1").strip("/")
    if prefix and not base.endswith(f"/{prefix}"):
        base = f"{base}/{prefix}"
    return f"{base}/{endpoint.lstrip('/')}"


@dataclass
class ProposalImageAsset:
    path: str
    status: str
    validation_reason: str = ""
    attachable: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "validation_reason": self.validation_reason,
            "attachable": self.attachable,
        }


class ProposalImageGenerator:
    def __init__(self, output_dir: Path = PROPOSAL_ASSETS_DIR):
        self.output_dir = output_dir

    @property
    def enabled(self) -> bool:
        return _env_bool("PROPOSAL_IMAGE_ENABLED", False)

    @property
    def attach_confirmed(self) -> bool:
        return _env_bool("KWORK_IMAGE_ATTACH_CONFIRMED", False)

    def should_generate(self, *, ai_score: int | float | None, vet_score: int | float | None) -> bool:
        if not self.enabled:
            return False
        min_ai = _env_float("PROPOSAL_IMAGE_MIN_AI_SCORE", 8)
        min_vet = _env_float("PROPOSAL_IMAGE_MIN_VET_SCORE", 70)
        return float(ai_score or 0) >= min_ai and float(vet_score or 0) >= min_vet

    async def maybe_generate(
        self,
        project: ProjectItem,
        proposal_text: str,
        *,
        ai_score: int | float | None,
        vet_score: int | float | None,
    ) -> ProposalImageAsset | None:
        if not self.should_generate(ai_score=ai_score, vet_score=vet_score):
            return None

        started_at = time.perf_counter()
        try:
            prompt = self._build_prompt(project, proposal_text)
            image_bytes = await self._generate_image_bytes(prompt)
            path = self._artifact_path(project)
            ensure_parent(path).write_bytes(image_bytes)
            ok, reason = await self._validate_image(path, project, proposal_text)
            status = "validated" if ok else "validation_failed"
            attachable = bool(ok and self.attach_confirmed)
            asset = ProposalImageAsset(
                path=str(path),
                status=status,
                validation_reason=reason,
                attachable=attachable,
            )
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            logger.info(
                f"ProposalImage: project={project.id} status={status} "
                f"attachable={attachable} elapsed={elapsed_ms}ms"
            )
            return asset
        except Exception as exc:
            logger.warning(f"ProposalImage: generation skipped for project={project.id}: {exc}")
            return ProposalImageAsset(path="", status="error", validation_reason=str(exc), attachable=False)

    def _artifact_path(self, project: ProjectItem) -> Path:
        safe_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", project.id or "project")
        return self.output_dir / f"{project.platform}_{safe_id}.png"

    def _build_prompt(self, project: ProjectItem, proposal_text: str) -> str:
        description = re.sub(r"\s+", " ", project.description or "")[:1200]
        proposal = re.sub(r"\s+", " ", proposal_text or "")[:800]
        return (
            "Create a clean, practical preview image for a freelance proposal. "
            "No logos, no brand names, no UI text blocks, no fake guarantees, no watermarks. "
            "The image should visually match the task and look like a useful concept/mockup, "
            "not a marketing poster.\n\n"
            f"Project title: {project.title}\n"
            f"Project description: {description}\n"
            f"Proposal angle: {proposal}\n"
        )

    async def _generate_image_bytes(self, prompt: str) -> bytes:
        api_key = _first_openai_key()
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY/OPENAI_API_KEYS is required for proposal images")

        payload = {
            "model": os.getenv("PROPOSAL_IMAGE_MODEL", "gpt-image-2"),
            "prompt": prompt,
            "size": os.getenv("PROPOSAL_IMAGE_SIZE", "1024x1024"),
            "quality": os.getenv("PROPOSAL_IMAGE_QUALITY", "low"),
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=90.0, trust_env=False) as client:
            response = await client.post(_openai_url("images/generations"), headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()

        b64 = self._extract_b64_image(data)
        if not b64:
            raise RuntimeError("image API returned no base64 image")
        return base64.b64decode(b64)

    @staticmethod
    def _extract_b64_image(data: dict[str, Any]) -> str:
        items = data.get("data")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and item.get("b64_json"):
                    return str(item["b64_json"])

        output = data.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                for content in item.get("content") or []:
                    if isinstance(content, dict) and content.get("b64_json"):
                        return str(content["b64_json"])
                    if isinstance(content, dict) and content.get("image_base64"):
                        return str(content["image_base64"])
        return ""

    async def _validate_image(
        self,
        path: Path,
        project: ProjectItem,
        proposal_text: str,
    ) -> tuple[bool, str]:
        if path.stat().st_size < 1024:
            return False, "image file is too small"

        api_key = _first_openai_key()
        if not api_key:
            return False, "OpenAI key missing for validation"

        image_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        prompt = (
            "Check whether this generated proposal image is safe to attach to a Kwork response. "
            "Return strict JSON only: {\"ok\":true|false,\"reason\":\"...\"}. "
            "Reject if it has broken text, random text, logos, third-party brands, unrealistic promises, "
            "or does not match the project.\n\n"
            f"Project: {project.title}\n"
            f"Proposal: {proposal_text[:1000]}"
        )
        payload = {
            "model": os.getenv("PROPOSAL_IMAGE_VALIDATION_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.5")),
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": f"data:image/png;base64,{image_b64}"},
                    ],
                }
            ],
            "max_output_tokens": 600,
            "store": False,
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            response = await client.post(_openai_url("responses"), headers=headers, json=payload)
            response.raise_for_status()
            text = OpenAICompatibleClient._extract_text(response.json())

        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return False, "validator returned non-json"
        import json

        data = json.loads(match.group())
        return bool(data.get("ok")), str(data.get("reason") or "")
