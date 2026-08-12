"""Kwork draft generation and guarded publication helpers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from src.paths import PROPOSAL_ASSETS_DIR, ensure_parent
from src.platforms.kwork_form_contract import attribute_manifest_hash, normalize_attribute_selection
from src.platforms.kwork_listing import KworkWebListingClient, selected_attribute_ids

API_BASE_URL = os.getenv("PSR_API_PUBLIC_BASE", "http://127.0.0.1:7788").rstrip("/")
_PUBLISH_CONFIRM_SECRET = secrets.token_hex(32)
PUBLISH_CONFIRMATION_PHRASE = "ОПУБЛИКОВАТЬ"
KWORK_IMAGE_MODEL = "gpt-image-2"
KWORK_VISUAL_PIPELINE_VERSION = "gpt-image-2-concrete-v3"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip())[:60].strip("-")
    return slug or str(int(time.time()))


def _truncate(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _extract_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


def _normalized_draft_selection(draft: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Use a stored manifest contract whenever a draft carries one."""

    selection = draft.get("attribute_selection") or draft.get("attributes") or {}
    if not isinstance(selection, dict):
        selection = {}
    manifest = draft.get("attribute_manifest")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("controls"), list):
        return selection, None
    normalized = normalize_attribute_selection(manifest, selection)
    return dict(normalized["selection"]), normalized


def _image_connection() -> dict[str, Any]:
    raw = os.getenv("KWORK_COVER_IMAGE_CONN") or os.getenv("OPENAI_IMAGE_CONN") or ""
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        return {}


def _image_api_key(fallback_key: str = "") -> str:
    conn = _image_connection()
    return (
        os.getenv("KWORK_COVER_IMAGE_API_KEY", "").strip()
        or str(conn.get("key") or "").strip()
        or os.getenv("OPENAI_IMAGE_API_KEY", "").strip()
        or fallback_key
    )


def _openai_api_keys() -> list[str]:
    raw_keys = os.getenv("OPENAI_API_KEYS", "")
    for delimiter in (";", "\n", "\r", "\t"):
        raw_keys = raw_keys.replace(delimiter, ",")
    candidates = [os.getenv("OPENAI_API_KEY", "").strip()]
    candidates.extend(item.strip() for item in raw_keys.split(",") if item.strip())
    result: list[str] = []
    for key in candidates:
        if key and key not in result:
            result.append(key)
    return result


def _image_api_url(endpoint: str, fallback_url: str) -> str:
    conn = _image_connection()
    base = (
        (
            os.getenv("KWORK_COVER_IMAGE_BASE_URL")
            or str(conn.get("url") or "")
            or os.getenv("OPENAI_IMAGE_BASE_URL")
            or ""
        )
        .strip()
        .rstrip("/")
    )
    if not base:
        base = fallback_url.rsplit("/images/generations", 1)[0].rstrip("/")
    prefix = (os.getenv("KWORK_COVER_IMAGE_API_PREFIX") or os.getenv("OPENAI_IMAGE_API_PREFIX") or "/v1").strip("/")
    if prefix and not base.endswith(f"/{prefix}"):
        base = f"{base}/{prefix}"
    return f"{base}/{endpoint.lstrip('/')}"


def _image_timeout_seconds() -> float:
    try:
        return float(os.getenv("KWORK_COVER_IMAGE_TIMEOUT", "240"))
    except (TypeError, ValueError):
        return 240.0


def _image_model() -> str:
    configured = os.getenv("KWORK_COVER_IMAGE_MODEL", KWORK_IMAGE_MODEL).strip() or KWORK_IMAGE_MODEL
    if configured != KWORK_IMAGE_MODEL:
        raise ValueError(
            f"KWORK_COVER_IMAGE_MODEL must be {KWORK_IMAGE_MODEL}; configured value is {configured!r}"
        )
    return configured


def _image_settings(purpose: str = "cover") -> dict[str, str]:
    if purpose == "portfolio":
        size = os.getenv("KWORK_PORTFOLIO_IMAGE_SIZE", os.getenv("KWORK_COVER_IMAGE_SIZE", "1536x1024"))
        quality = os.getenv(
            "KWORK_PORTFOLIO_IMAGE_QUALITY",
            os.getenv("KWORK_COVER_IMAGE_QUALITY", "high"),
        )
    else:
        size = os.getenv("KWORK_COVER_IMAGE_SIZE", "1536x1024")
        quality = os.getenv("KWORK_COVER_IMAGE_QUALITY", "high")
    return {
        "model": _image_model(),
        "size": str(size or "1536x1024").strip(),
        "quality": str(quality or "high").strip(),
    }


def _cover_qa_enabled() -> bool:
    return os.getenv("KWORK_COVER_QA_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def _cover_qa_min_score() -> float:
    try:
        return max(0.0, min(10.0, float(os.getenv("KWORK_COVER_QA_MIN_SCORE", "7"))))
    except (TypeError, ValueError):
        return 7.0


def _responses_output_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    chunks: list[str] = []
    for output in payload.get("output") or []:
        if not isinstance(output, dict):
            continue
        for content in output.get("content") or []:
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
    return "\n".join(chunks)


def _competitor_cover_limit() -> int:
    try:
        return max(0, min(8, int(os.getenv("KWORK_COMPETITOR_COVER_LIMIT", "5") or "5")))
    except (TypeError, ValueError):
        return 5


def _cover_history_limit() -> int:
    try:
        return max(0, min(8, int(os.getenv("KWORK_COVER_HISTORY_LIMIT", "4") or "4")))
    except (TypeError, ValueError):
        return 4


def _vision_timeout_seconds() -> float:
    try:
        return float(os.getenv("KWORK_COVER_VISION_TIMEOUT", "60"))
    except (TypeError, ValueError):
        return 60.0


def _competitor_cover_items(request: dict[str, Any], limit: int | None = None) -> list[dict[str, str]]:
    market_context = request.get("market_context") if isinstance(request.get("market_context"), dict) else {}
    competitors = market_context.get("competitors") or market_context.get("practice_context") or []
    if not isinstance(competitors, list):
        return []
    max_items = _competitor_cover_limit() if limit is None else limit
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in competitors:
        if not isinstance(item, dict):
            continue
        portfolio_images = item.get("portfolio_images") if isinstance(item.get("portfolio_images"), list) else []
        sources = [item.get("image_url") or item.get("cover"), *portfolio_images]
        for index, source in enumerate(sources):
            image_url = str(source or "").strip()
            if not image_url or image_url in seen:
                continue
            seen.add(image_url)
            result.append(
                {
                    "title": str(item.get("title") or "")[:160],
                    "image_url": image_url,
                    "price": str(item.get("price") or ""),
                    "service_size": str(item.get("service_size") or ""),
                    "reference_kind": "cover" if index == 0 else "portfolio",
                }
            )
            if len(result) >= max_items:
                return result
    return result


def _image_bytes_to_data_url(raw: bytes, content_type: str = "") -> str:
    mime = (content_type or "image/jpeg").split(";", 1)[0].strip().lower()
    try:
        from PIL import Image

        with Image.open(BytesIO(raw)) as image:
            image.load()
            image = image.convert("RGB")
            image.thumbnail((768, 512))
            output = BytesIO()
            image.save(output, format="JPEG", quality=82, optimize=True)
            raw = output.getvalue()
            mime = "image/jpeg"
    except Exception:
        if mime not in {"image/jpeg", "image/jpg", "image/png", "image/webp"}:
            mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _image_file_to_data_url(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except Exception:
        return ""
    return _image_bytes_to_data_url(raw, "image/png")


def _cover_sidecar_path(path: Path) -> Path:
    return path.with_suffix(".json")


def _cover_prompt_context(
    visual_analysis: dict[str, Any],
    cover_history: list[dict[str, Any]],
    *,
    prompt_images: list[str] | None = None,
    competitor_images_sent: int | None = None,
    history_images_sent: int | None = None,
    route: str = "",
    warning: str = "",
) -> dict[str, Any]:
    competitor_data_urls = int(visual_analysis.get("images_seen") or 0)
    history_data_urls = len([item for item in cover_history if item.get("data_url")])
    return {
        "prompt_writer_route": route,
        "competitor_images_downloaded": competitor_data_urls,
        "competitor_images_sent": competitor_data_urls if competitor_images_sent is None else competitor_images_sent,
        "history_images_sent": history_data_urls if history_images_sent is None else history_images_sent,
        "prompt_images_sent": len(prompt_images or []),
        "recent_history_count": len(cover_history),
        "competitor_image_urls": visual_analysis.get("image_urls") or [],
        "recent_cover_titles": [
            str(item.get("title") or item.get("path") or "")[:120]
            for item in cover_history[:4]
            if item.get("title") or item.get("path")
        ],
        "warning": warning,
    }


def _sanitize_kwork_text(value: Any) -> str:
    text = str(value or "").strip()
    replacements = (
        (r"\bобсуд(?:ить|им)\s+до\s+заказа\b", "уточнить в рамках заказа"),
        (r"\bсвяз(?:аться|аться со мной)\s+до\s+заказа\b", "задать вопросы в чате заказа"),
        (r"\b(?:напишите|пишите|спросите)\s+до\s+заказа\b", "задайте вопросы в чате заказа"),
        (r"\bдо\s+заказа\b", "перед началом работы"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.I)
    return text


_VISUAL_DOMAIN_RULES: tuple[dict[str, Any], ...] = (
    {
        "id": "web_software",
        "keywords": (
            "сайт", "лендинг", "интерфейс", "приложен", "бот", "скрипт", "код", "web", "website",
            "landing", "frontend", "backend", "software", "telegram", "ui", "ux", "програм",
        ),
        "artifact": "one coherent, already-shipped website, application screen, automation result, or developer tool",
        "process": "a real developer working with the specific product on one clear screen in a believable workspace",
        "outcome": "a real user successfully using the finished digital product in context",
        "style": "crisp product design, functional spacing, believable UI hierarchy, restrained contemporary palette",
        "allow_embedded_text": True,
        "avoid": "code rain, digital brains, neon cubes, generic dashboards, floating screens, device stacks",
    },
    {
        "id": "branding_design",
        "keywords": (
            "логотип", "бренд", "айдентик", "шрифт", "упаков", "иллюстрац", "баннер", "дизайн",
            "brand", "logo", "identity", "typography", "packaging", "graphic design",
        ),
        "artifact": "the finished identity, packaging, illustration, layout, or branded object used on a realistic surface",
        "process": "a designer refining the actual asset with relevant tools and physical or digital materials",
        "outcome": "the finished design working naturally in its intended customer-facing environment",
        "style": "editorial, tactile, material-aware art direction with controlled light and precise composition",
        "allow_embedded_text": True,
        "avoid": "presentation boards, alphabet sheets, generic mockups, moodboards, floating swatches",
    },
    {
        "id": "marketing_content",
        "keywords": (
            "маркет", "реклам", "seo", "smm", "соцсет", "контент", "продвиж", "воронк", "таргет",
            "marketing", "advert", "campaign", "social media", "content", "promotion",
        ),
        "artifact": "one finished campaign asset or content experience shown at a realistic publication scale",
        "process": "a specialist producing the concrete campaign with real source material and one focused workspace",
        "outcome": "the target audience engaging with the finished campaign in a believable channel",
        "style": "clear editorial hierarchy, culturally current imagery, energetic but controlled commercial direction",
        "allow_embedded_text": True,
        "avoid": "megaphones, rockets, random analytics panels, growth arrows, generic influencer collages",
    },
    {
        "id": "writing_translation",
        "keywords": (
            "текст", "копирай", "перевод", "редакт", "статья", "сценар", "книга", "резюме", "writing",
            "translation", "copywriting", "editing", "article", "script",
        ),
        "artifact": "a polished document, editorial spread, article, script, or publication in its real reading context",
        "process": "an editor or writer working on the actual document with visible structure and source material",
        "outcome": "a reader using the finished publication comfortably in a realistic setting",
        "style": "calm editorial realism, refined paper or screen texture, natural light, strong reading hierarchy",
        "allow_embedded_text": True,
        "avoid": "floating letters, quills, random books, word clouds, generic laptop stock photography",
    },
    {
        "id": "video_audio",
        "keywords": (
            "видео", "монтаж", "монтир", "моушн", "анимац", "озвуч", "аудио", "музык", "звук", "ролик",
            "video", "audio", "editing", "animation", "motion design", "voiceover", "music", "sound",
        ),
        "artifact": "one strong finished frame, listening experience, or production result that represents the service",
        "process": "a creator editing the actual footage or audio in a focused studio with believable equipment",
        "outcome": "the finished media being watched or heard in the intended real-world context",
        "style": "cinematic realism, purposeful lighting, authentic equipment, precise focal depth",
        "allow_embedded_text": False,
        "avoid": "floating play buttons, giant waveforms, generic headphones, timeline collages, neon equalizers",
    },
    {
        "id": "legal_finance_business",
        "keywords": (
            "юрист", "право", "договор", "бухгал", "финанс", "налог", "бизнес", "консалт", "legal",
            "law", "contract", "accounting", "finance", "tax", "consulting",
        ),
        "artifact": "the concrete professional result: a reviewed document, financial model, report, or decision-ready material",
        "process": "a specialist and client working through the actual documents or figures in a credible office setting",
        "outcome": "a client confidently using the completed professional deliverable",
        "style": "quiet professional realism, natural materials, daylight, credible detail, understated confidence",
        "allow_embedded_text": False,
        "avoid": "gavels, scales of justice, handshakes, skyscrapers, chess pieces, generic corporate metaphors",
    },
    {
        "id": "education",
        "keywords": (
            "обуч", "урок", "курс", "репетитор", "домашн", "экзамен", "education", "lesson", "course",
            "tutor", "training",
        ),
        "artifact": "the concrete lesson material, exercise, experiment, or learning tool used in context",
        "process": "a teacher and learner actively solving the specific task with relevant materials",
        "outcome": "a learner successfully applying the new skill in a realistic situation",
        "style": "warm documentary realism, clear gestures, authentic materials, inclusive natural atmosphere",
        "allow_embedded_text": False,
        "avoid": "light bulbs, floating books, graduation caps, generic classrooms, decorative equations",
    },
    {
        "id": "beauty_wellness",
        "keywords": (
            "космет", "макияж", "кожа", "волос", "маникюр", "массаж", "beauty", "cosmetic", "makeup",
            "skin", "hair", "nail", "wellness",
        ),
        "artifact": "the finished treatment or product result shown close enough to inspect real texture",
        "process": "the treatment being performed with authentic tools, skin, hair, or product texture",
        "outcome": "a real person naturally experiencing the finished result without staged glamour posing",
        "style": "tactile premium editorial photography, real skin texture, controlled soft light, clean material detail",
        "allow_embedded_text": False,
        "avoid": "spa stones, random flowers, floating bottles, fake packaging, plastic skin",
    },
    {
        "id": "fitness_health",
        "keywords": (
            "фитнес", "тренер", "спорт", "трениров", "йога", "питани", "здоров", "fitness", "workout",
            "sport", "training", "yoga", "health",
        ),
        "artifact": "the concrete training plan, exercise setup, or health service result in real use",
        "process": "a person performing the specific movement with believable effort, breathing, equipment, and anatomy",
        "outcome": "a client using the program confidently in an authentic everyday environment",
        "style": "dynamic documentary photography, physical realism, natural skin, motion, and directional light",
        "allow_embedded_text": False,
        "avoid": "posing fitness models, impossible anatomy, empty gyms, motivational posters, glowing body graphics",
    },
    {
        "id": "food_hospitality",
        "keywords": (
            "еда", "блюд", "ресторан", "кафе", "повар", "рецепт", "food", "dish", "restaurant", "cafe",
            "cooking", "menu", "hospitality",
        ),
        "artifact": "the finished dish, menu experience, table setting, or hospitality result shown appetizingly and honestly",
        "process": "the specific dish or service being prepared with real ingredients, steam, crumbs, and tools",
        "outcome": "a guest naturally enjoying the finished food or hospitality experience",
        "style": "tactile food editorial photography, directional natural light, believable texture and imperfection",
        "allow_embedded_text": False,
        "avoid": "flying ingredients, sterile tables, plastic perfection, generic chef portraits, decorative flat lays",
    },
    {
        "id": "architecture_interior",
        "keywords": (
            "интерьер", "архитект", "планиров", "ремонт", "визуализац", "мебел", "architecture", "interior",
            "renovation", "furniture", "render",
        ),
        "artifact": "one finished room, building detail, plan-to-space result, or furniture solution shown at inspectable scale",
        "process": "the specialist working with the real space, drawing, model, or materials",
        "outcome": "people naturally using the completed space as intended",
        "style": "architectural editorial photography, accurate perspective, material realism, natural daylight",
        "allow_embedded_text": False,
        "avoid": "floating floor plans, impossible rooms, generic luxury renders, material moodboards, split-screen makeovers",
    },
    {
        "id": "craft_product",
        "keywords": (
            "ручн", "издел", "дерев", "кож", "шить", "вяз", "керами", "ювелир", "craft", "handmade",
            "wood", "leather", "sewing", "ceramic", "jewelry", "product",
        ),
        "artifact": "the finished physical object shown close enough to inspect construction, material, and finish",
        "process": "the maker's hands, authentic tools, material, and the work in progress",
        "outcome": "the object being used naturally in the environment it was made for",
        "style": "tactile workshop editorial photography, honest material texture, warm directional light",
        "allow_embedded_text": False,
        "avoid": "tool moodboards, random flat lays, floating materials, generic artisan portraits, decorative collages",
    },
)

_GENERIC_VISUAL_KEYWORDS = {"дизайн", "design", "product", "content", "training"}


def _visual_keyword_matches(keyword: str, corpus: str) -> bool:
    if re.fullmatch(r"[a-z0-9]+(?: [a-z0-9]+)*", keyword):
        pattern = r"(?<![a-z0-9])" + re.escape(keyword).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
        return re.search(pattern, corpus) is not None
    return keyword in corpus


def _visual_strategy(draft: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    primary_corpus = " ".join(
        str(value or "")
        for value in (
            draft.get("title"),
            request.get("service_summary"),
            request.get("brief"),
            request.get("image_context"),
            request.get("portfolio_context"),
        )
    ).lower()
    supporting_corpus = str(draft.get("description") or "").lower()
    taxonomy_corpus = " ".join(
        str(value or "")
        for value in (
            request.get("category_name"),
            request.get("classifier_name"),
            draft.get("category_name"),
            draft.get("classifier_name"),
        )
    ).lower()
    corpus = f"{primary_corpus} {taxonomy_corpus}"
    scored = [
        (
            sum(
                1 if keyword in _GENERIC_VISUAL_KEYWORDS else 3
                for keyword in rule["keywords"]
                if _visual_keyword_matches(keyword, primary_corpus)
            )
            + sum(1 for keyword in rule["keywords"] if _visual_keyword_matches(keyword, supporting_corpus))
            + sum(1 for keyword in rule["keywords"] if _visual_keyword_matches(keyword, taxonomy_corpus)),
            index,
            rule,
        )
        for index, rule in enumerate(_VISUAL_DOMAIN_RULES)
    ]
    score, _, selected = max(scored, key=lambda item: (item[0], -item[1]))
    if score <= 0:
        selected = {
            "id": "professional_service",
            "artifact": "the concrete finished deliverable or tangible result of the exact service",
            "process": "a real specialist performing the exact task with relevant tools and source material",
            "outcome": "a real client using the finished result in its intended context",
            "style": "ambient professional realism, natural light, believable detail, calm asymmetry",
            "allow_embedded_text": False,
            "avoid": "generic office stock, handshakes, abstract metaphors, decorative icons, anonymous dashboards",
        }
    modes = ("artifact", "process", "outcome")
    requested_mode = str(request.get("scene_mode") or "").strip().lower()
    mode = requested_mode if requested_mode in modes else modes[int(request.get("variant_index") or 0) % len(modes)]
    experimental = any(
        marker in corpus
        for marker in ("experimental", "эксперимент", "surreal", "сюрреал", "conceptual", "концептуал")
    )
    style = str(selected["style"])
    if experimental:
        style += "; the explicitly requested experimental direction may bend realism while preserving the real deliverable"
    return {
        "domain": selected["id"],
        "scene_mode": mode,
        "visual_proof": selected[mode],
        "style": style,
        "allow_embedded_text": bool(selected["allow_embedded_text"]),
        "avoid": selected["avoid"],
        "experimental_requested": experimental,
    }


def _scene_subject_constraints(strategy: dict[str, Any]) -> str:
    if strategy.get("domain") == "web_software" and strategy.get("scene_mode") == "artifact":
        return (
            "The digital product itself must fill the canvas as a direct screenshot-like interface view. "
            "Do not show a person, face, hand, monitor, laptop, phone, desk, room, keyboard, books, coffee, "
            "plants, or any lifestyle props."
        )
    if strategy.get("scene_mode") == "artifact":
        return (
            "Frame the exact finished deliverable itself as the dominant subject. Exclude unrelated people, "
            "rooms, workspaces, and decorative props unless they are intrinsic to the deliverable."
        )
    return "Every person, tool, environment, and prop must be necessary evidence of the exact service."


def _portfolio_image_specs(
    draft: dict[str, Any],
    request: dict[str, Any],
    *,
    cover_prompt: str = "",
    count: int = 5,
) -> list[dict[str, Any]]:
    strategy = _visual_strategy(draft, request)
    service = str(draft.get("title") or request.get("service_summary") or "the service").strip()
    category = str(request.get("category_name") or draft.get("category_name") or "").strip()
    embedded_text = (
        "Text may appear only where it naturally belongs inside the depicted deliverable; never add a cover headline, caption, label, email address, URL, domain, phone number, @handle, QR code, or contact detail."
        if strategy["allow_embedded_text"]
        else "Do not render readable letters, words, numbers, labels, or signage."
    )
    views = (
        ("Основной результат", "Готовая работа целиком", "artifact", "show the strongest complete deliverable as the single dominant subject"),
        ("Ключевая деталь", "Качество крупным планом", "artifact", "show one inspectable detail that proves craft, quality, or functionality"),
        ("В рабочем контексте", "Результат в реальном использовании", "outcome", "show the finished result being used naturally in its intended environment"),
        ("Процесс работы", "Реальные инструменты и действие", "process", "show the authentic process, tools, material, or interaction behind the service"),
        ("Финальная подача", "Ещё один убедительный ракурс", "artifact", "show an alternate but coherent final view without repeating the cover composition"),
        ("Исходные материалы", "Контекст до начала работы", "process", "show the authentic source material or starting condition without a before-and-after layout"),
        ("Проверка качества", "Результат при внимательном осмотре", "artifact", "show a different inspectable proof of finish, accuracy, or function"),
        ("Сценарий использования", "Практическая ценность результата", "outcome", "show another realistic use case with a clearly different composition"),
        ("Рабочая среда", "Результат в естественном окружении", "outcome", "show the deliverable integrated naturally into its intended environment"),
        ("Альтернативный ракурс", "Дополнительное подтверждение качества", "artifact", "show a final distinct angle, crop, or state that adds new evidence"),
    )
    specs: list[dict[str, Any]] = []
    for index, (title, subtitle, mode, focus) in enumerate(views[:count]):
        mode_strategy = _visual_strategy(draft, {**request, "scene_mode": mode})
        prompt = " ".join(
            (
                "Create one production-quality 3:2 landscape portfolio visual.",
                f"Exact service: {service}.",
                f"Category context: {category}." if category else "",
                f"Domain: {strategy['domain']}.",
                f"Visual proof: {mode_strategy['visual_proof']}; {focus}.",
                f"Art direction: {strategy['style']}.",
                _scene_subject_constraints(mode_strategy),
                "The image is the final visual itself, edge to edge, with one coherent scene and one primary focal point.",
                "Keep the important subject clear at thumbnail size and inside the 3:2 safe area.",
                embedded_text,
                "No presentation board, case-study slide, moodboard, contact sheet, split screen, before-and-after, multi-panel collage, device stack, floating screens, decorative border, poster frame, or card-within-a-card.",
                "No abstract geometric composition, floating cubes, glowing blobs, gradient ribbons, generic symbolism, logos, brand names, contacts, or watermarks.",
                f"Domain-specific exclusions: {strategy['avoid']}.",
                "Use plausible anatomy, perspective, scale, lighting, shadows, materials, and interface structure.",
                f"Keep the art-direction quality of this cover prompt without copying its layout: {_truncate(cover_prompt, 700)}"
                if cover_prompt
                else "",
                f"Portfolio view {index + 1} of {count}; make it meaningfully distinct from the other views.",
            )
        )
        specs.append(
            {
                "title": title,
                "subtitle": subtitle,
                "scene_mode": mode,
                "visual_strategy": mode_strategy,
                "prompt": " ".join(prompt.split()),
            }
        )
    return specs


def _required_portfolio_count(draft: dict[str, Any]) -> int:
    explicit = draft.get("portfolio_required_count")
    if explicit not in (None, ""):
        try:
            return max(0, min(10, int(explicit)))
        except (TypeError, ValueError):
            pass
    manifest = draft.get("attribute_manifest") if isinstance(draft.get("attribute_manifest"), dict) else {}
    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    for key in ("portfolio_required_count", "required_portfolio_count", "min_portfolio_count"):
        value = metadata.get(key)
        if value not in (None, ""):
            try:
                return max(0, min(10, int(value)))
            except (TypeError, ValueError):
                continue
    return 5 if int(draft.get("category_id") or 0) == 25 else 0


def _render_portfolio_assets(draft: dict[str, Any], *, count: int = 5) -> list[dict[str, Any]]:
    existing = draft.get("portfolio_assets") if isinstance(draft.get("portfolio_assets"), list) else []
    reusable = [
        dict(item)
        for item in existing
        if isinstance(item, dict)
        and item.get("path")
        and Path(str(item["path"])).is_file()
        and item.get("visual_pipeline_version") == KWORK_VISUAL_PIPELINE_VERSION
        and isinstance(item.get("image_generation"), dict)
        and item["image_generation"].get("requested_model") == KWORK_IMAGE_MODEL
        and (
            not _cover_qa_enabled()
            or (
                isinstance(item.get("quality_gate"), dict)
                and item["quality_gate"].get("status") == "passed"
            )
        )
    ]
    return reusable[:count]


def _portfolio_payload(upload: dict[str, Any], title: str, index: int) -> dict[str, Any]:
    media_id = int(upload["id"])
    item_crop = upload.get("crop") or {"x": 0, "y": 0, "w": 1, "h": 1}
    return {
        "id": None,
        "draftHash": int(time.time() * 1000) + index,
        "cover": {
            "id": None,
            "crop": item_crop,
            "type": "image",
            "idPortfolioMedia": media_id,
        },
        "title": _truncate(title, 80),
        "description": "",
        "items": [
            {
                "id": media_id,
                "crop": item_crop,
                "position": 0,
                "portfolio_type": "photo",
            }
        ],
    }


def _default_cover_crop(path: str | Path) -> dict[str, int]:
    """Return a center 3:2 crop payload compatible with Kwork cover fields."""
    width, height = 1200, 800
    try:
        from PIL import Image

        with Image.open(path) as image:
            width, height = image.size
    except Exception:
        pass

    target_ratio = 3 / 2
    if width / max(1, height) > target_ratio:
        crop_h = height
        crop_w = int(height * target_ratio)
    else:
        crop_w = width
        crop_h = int(width / target_ratio)
    crop_w = max(1, min(width, crop_w))
    crop_h = max(1, min(height, crop_h))
    x = max(0, (width - crop_w) // 2)
    y = max(0, (height - crop_h) // 2)
    return {
        "x": x,
        "y": y,
        "w": crop_w,
        "h": crop_h,
        "x1": x,
        "y1": y,
        "x2": x + crop_w,
        "y2": y + crop_h,
        "minW": min(660, crop_w),
        "minH": min(440, crop_h),
    }


def _normalize_cover_png(path: Path) -> tuple[bool, str]:
    try:
        from PIL import Image
    except Exception as exc:
        return False, f"Pillow unavailable: {exc}"

    try:
        with Image.open(path) as image:
            image.load()
            image = image.convert("RGB")
            width, height = image.size
            if width < 660 or height < 440:
                return False, f"cover is too small: {width}x{height}"

            target_ratio = 3 / 2
            current_ratio = width / height
            if current_ratio > target_ratio:
                crop_w = int(height * target_ratio)
                left = max(0, (width - crop_w) // 2)
                image = image.crop((left, 0, left + crop_w, height))
            elif current_ratio < target_ratio:
                crop_h = int(width / target_ratio)
                top = max(0, (height - crop_h) // 2)
                image = image.crop((0, top, width, top + crop_h))

            image.save(path, "PNG", optimize=True)
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


class KworkAutopublishService:
    """Generate Kwork listing drafts and build guarded save payloads."""

    @staticmethod
    def ensure_portfolio_assets(draft: dict[str, Any]) -> list[dict[str, Any]]:
        count = _required_portfolio_count(draft)
        return _render_portfolio_assets(draft, count=count) if count else []

    async def generate_draft(self, request: dict[str, Any]) -> dict[str, Any]:
        category_id = int(request.get("category_id") or 0)
        if category_id <= 0:
            raise ValueError("category_id is required")

        draft = await self._generate_text(request)
        draft.update(
            {
                "category_id": category_id,
                "category_name": request.get("category_name") or "",
                "classifier_id": request.get("classifier_id"),
                "classifier_name": request.get("classifier_name") or "",
                "variant_index": int(request.get("variant_index") or 0),
                "variant_count": max(1, min(3, int(request.get("variant_count") or 1))),
                "creative_direction": str(request.get("creative_direction") or "").strip(),
                "price": int(request.get("price") or draft.get("price") or 500),
                "work_time": int(request.get("work_time") or draft.get("work_time") or 3),
                "attributes": request.get("attributes") or {},
                "attribute_manifest": request.get("attribute_manifest") or {},
                "attribute_selection": request.get("attribute_selection") or request.get("attributes") or {},
                "lang": request.get("lang") or "ru",
            }
        )
        draft["visual_strategy"] = _visual_strategy(draft, request)

        image = None
        if request.get("generate_image"):
            image = await self.generate_cover(draft, request)
            if isinstance(image, dict) and image.get("path"):
                draft["cover_image_path"] = image.get("path")
                draft["cover_image"] = image
                if _required_portfolio_count(draft):
                    assets = await self.generate_portfolio_assets(
                        draft,
                        request,
                        cover_prompt=str(image.get("prompt") or ""),
                    )
                    draft["portfolio_assets"] = assets
                    if request.get("_portfolio_generation_errors"):
                        draft["portfolio_generation_errors"] = request["_portfolio_generation_errors"]

        return {"ok": True, "draft": draft, "image": image}

    async def _generate_text(self, request: dict[str, Any]) -> dict[str, Any]:
        fallback = self._fallback_draft(request)
        if request.get("use_llm") is False:
            return fallback

        try:
            from src.brain.llm_router import get_llm_router

            router = get_llm_router()
            prompt = self._build_generation_prompt(request)
            response = await router.generate(
                prompt=prompt,
                provider=(request.get("provider") or os.getenv("KWORK_AUTOPUBLISH_PROVIDER") or None),
                model=request.get("model") or os.getenv("KWORK_AUTOPUBLISH_MODEL") or None,
                temperature=float(request.get("temperature") or 0.45),
                max_tokens=1800,
                task="kwork_autopublish",
                system_prompt=(
                    "You create compliant Kwork service listing drafts. "
                    "Return only valid JSON. Do not include contacts, guarantees of impossible results, or policy-unsafe claims."
                ),
            )
            parsed = _extract_json_object(response)
            if not parsed:
                return fallback
            return self._normalize_draft(parsed, fallback)
        except Exception as exc:
            logger.warning(f"KworkAutopublish: LLM draft generation fallback: {exc}")
            return fallback

    def _build_generation_prompt(self, request: dict[str, Any]) -> str:
        market_context = request.get("market_context") if isinstance(request.get("market_context"), dict) else {}
        competitors = market_context.get("competitors") or market_context.get("practice_context") or []
        if not isinstance(competitors, list):
            competitors = []
        return json.dumps(
            {
                "task": "Generate a Kwork listing draft in Russian.",
                "rules": [
                    "The user-defined price and work_time are fixed inputs; do not optimize or override them.",
                    "Use competitor examples only as market references and best practices. Do not copy text verbatim.",
                    "Prefer concrete scope, deliverables, buyer instructions, and limits.",
                    "If audience is empty, keep auditory empty. Do not invent a target audience.",
                    "When variant_count is greater than one, make this offer meaningfully different from the other variants while staying in the same market niche.",
                ],
                "category": {
                    "id": request.get("category_id"),
                    "name": request.get("category_name"),
                    "classifier_id": request.get("classifier_id"),
                    "classifier_name": request.get("classifier_name"),
                },
                "service_summary": request.get("service_summary") or request.get("brief") or "",
                "audience": request.get("audience") or "",
                "market_context": request.get("market_context") or {},
                "competitor_examples": competitors[:6],
                "fixed_price": request.get("price"),
                "fixed_work_time": request.get("work_time"),
                "portfolio_context": request.get("portfolio_context") or "",
                "variant": {
                    "index": int(request.get("variant_index") or 0) + 1,
                    "count": max(1, min(3, int(request.get("variant_count") or 1))),
                    "creative_direction": request.get("creative_direction") or "",
                },
                "required_json_schema": {
                    "title": "string, <=80 chars",
                    "description": "string, 600-1200 chars",
                    "instruction": "string, what buyer should provide",
                    "auditory": "string, target audience; empty string when audience input is empty",
                    "service_size": "string",
                    "volume": "string",
                    "price": "integer rubles",
                    "work_time": "integer days",
                    "faq": [{"question": "string", "answer": "string"}],
                },
            },
            ensure_ascii=False,
        )

    def _recent_cover_history(
        self,
        draft: dict[str, Any],
        request: dict[str, Any],
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        max_items = _cover_history_limit() if limit is None else max(0, limit)
        if max_items <= 0:
            return []
        root = PROPOSAL_ASSETS_DIR / "kwork_autopublish"
        if not root.exists():
            return []

        target_category = str(request.get("category_name") or draft.get("category_name") or "").strip().lower()
        target_classifier = str(request.get("classifier_name") or draft.get("classifier_name") or "").strip().lower()

        def same_market(data: dict[str, Any]) -> bool:
            candidate_category = str(data.get("category") or "").strip().lower()
            candidate_classifier = str(data.get("classifier") or "").strip().lower()
            if target_classifier and candidate_classifier:
                return target_classifier in candidate_classifier or candidate_classifier in target_classifier
            if target_category and candidate_category:
                return target_category in candidate_category or candidate_category in target_category
            return not target_category and not target_classifier

        result: list[dict[str, Any]] = []
        for sidecar in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
            except Exception:
                continue
            if (
                not isinstance(data, dict)
                or data.get("asset_kind") not in (None, "cover")
                or data.get("status") != "generated"
                or data.get("visual_pipeline_version") != KWORK_VISUAL_PIPELINE_VERSION
                or not isinstance(data.get("image_generation"), dict)
                or data["image_generation"].get("requested_model") != KWORK_IMAGE_MODEL
                or not same_market(data)
            ):
                continue
            image_path = Path(str(data.get("path") or sidecar.with_suffix(".png")))
            if not image_path.is_file():
                image_path = sidecar.with_suffix(".png")
            if not image_path.is_file():
                continue
            result.append(
                {
                    "created_at": data.get("created_at"),
                    "status": data.get("status"),
                    "title": data.get("title"),
                    "category": data.get("category"),
                    "classifier": data.get("classifier"),
                }
            )
            if len(result) >= max_items:
                break
        return result

    def _write_cover_sidecar(
        self,
        path: Path,
        draft: dict[str, Any],
        request: dict[str, Any],
        *,
        prompt: str,
        prompt_source: str,
        status: str,
        detail: str = "",
    ) -> None:
        analysis = (
            request.get("_cover_visual_analysis") if isinstance(request.get("_cover_visual_analysis"), dict) else {}
        )
        payload = {
            "asset_kind": "cover",
            "visual_pipeline_version": KWORK_VISUAL_PIPELINE_VERSION,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "status": status,
            "path": str(path),
            "title": draft.get("title"),
            "category": request.get("category_name"),
            "classifier": request.get("classifier_name"),
            "service_summary": request.get("service_summary") or request.get("brief") or "",
            "audience": request.get("audience") or draft.get("auditory") or "",
            "prompt": prompt,
            "prompt_source": prompt_source,
            "detail": detail,
            "visual_strategy": request.get("_cover_visual_strategy") or _visual_strategy(draft, request),
            "image_generation": request.get("_cover_image_settings") or {},
            "quality_gate": request.get("_cover_quality_gate") or {},
            "visual_analysis_status": analysis.get("status"),
            "visual_style_brief": analysis.get("brief"),
            "competitor_image_urls": analysis.get("image_urls") or [],
            "competitor_images_seen": analysis.get("images_seen") or 0,
            "cover_prompt_context": request.get("_cover_prompt_context") or {},
        }
        try:
            ensure_parent(_cover_sidecar_path(path)).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.debug(f"KworkAutopublish: cover sidecar write failed: {exc}")

    async def build_cover_prompt(self, draft: dict[str, Any], request: dict[str, Any]) -> tuple[str, str]:
        visual_analysis = request.get("_cover_visual_analysis")
        if not isinstance(visual_analysis, dict):
            visual_analysis = await self.analyze_competitor_covers(draft, request)
        request["_cover_visual_analysis"] = visual_analysis
        request["_cover_visual_strategy"] = _visual_strategy(draft, request)
        cover_history = self._recent_cover_history(draft, request)
        request["_cover_history"] = [
            {
                key: item.get(key)
                for key in ("created_at", "status", "title", "category", "classifier")
                if item.get(key) not in (None, "")
            }
            for item in cover_history
        ]
        fallback = self._fallback_cover_prompt(draft, request, visual_analysis=visual_analysis)
        request["_cover_prompt_context"] = _cover_prompt_context(
            visual_analysis,
            cover_history,
            route="fallback_disabled" if request.get("use_cover_prompt_llm") is False else "pending",
        )
        if request.get("use_cover_prompt_llm") is False:
            return fallback, "fallback"

        try:
            from src.brain.llm_router import get_llm_router

            router = get_llm_router()
            provider = (
                request.get("cover_prompt_provider")
                or os.getenv("KWORK_COVER_PROMPT_PROVIDER")
                or request.get("provider")
                or None
            )
            model = (
                request.get("cover_prompt_model")
                or os.getenv("KWORK_COVER_PROMPT_MODEL")
                or request.get("model")
                or None
            )
            strategy = _visual_strategy(draft, request)
            request["_cover_visual_strategy"] = strategy
            prompt_payload = json.dumps(
                self._cover_prompt_brief(draft, request, visual_analysis=visual_analysis), ensure_ascii=False
            )
            request["_cover_prompt_context"] = _cover_prompt_context(
                visual_analysis,
                cover_history,
                prompt_images=[],
                competitor_images_sent=0,
                history_images_sent=0,
                route="llm_text_no_images",
            )
            request["_cover_prompt_context"].update(
                {
                    "prompt_writer_provider": provider or "auto",
                    "prompt_writer_model": model or "auto",
                }
            )
            text_policy = (
                "Text may appear only where it naturally belongs inside the depicted deliverable. Never add a cover headline, caption, label, promotional typography, email address, URL, domain, phone number, @handle, QR code, or contact detail anywhere. "
                if strategy["allow_embedded_text"]
                else "The image must contain no readable words, letters, numbers, labels, or signage. "
            )
            system_prompt = (
                "You are a cross-domain art director writing one production prompt for GPT Image 2. "
                "Return plain English text only, no markdown and no JSON. Put all non-negotiable constraints near the start. "
                "The image must be one coherent edge-to-edge scene with one primary focal point, not a poster, case-study slide, presentation board, moodboard, contact sheet, split screen, before-and-after, or multi-panel collage. "
                "Show literal visual proof of the exact service: its real deliverable, authentic process, or real-world outcome. Style is treatment, never the subject. "
                f"Use this routed domain strategy: {json.dumps(strategy, ensure_ascii=False)}. "
                "Treat the textual competitor analysis as negative market evidence: avoid its identified clichés. Never imitate competitor layout, palette, typography, or props. "
                "Recent cover titles are repetition evidence only. Never use old generated images as positive style references. "
                f"Mandatory subject constraint: {_scene_subject_constraints(strategy)} "
                "For web or software outside artifact mode, show one convincing already-shipped interface or a real developer using the specific product; never simplified placeholder blocks, floating screens, device stacks, or generic tech metaphors. "
                "For physical, human, professional, educational, food, beauty, fitness, craft, media, and spatial services, choose concrete domain-specific subjects, actions, materials, environments, camera, and lighting. "
                "No added logos, brand names, contacts, watermarks, decorative frames, glowing geometry, floating cubes, gradient ribbons, generic icons, or card-within-a-card layouts. Contact details are forbidden even when they would naturally appear inside the depicted interface or document. "
                f"{text_policy}"
                "Describe subject, action, environment, composition, camera/framing, light, materials, palette, mood, and exclusions. Keep the final prompt under 2600 characters."
            )
            prompt_attempts = (
                max(1, min(3, len(_openai_api_keys())))
                if str(provider or "").strip().lower() == "openai"
                else 1
            )
            response = ""
            last_prompt_error: Exception | None = None
            for attempt in range(prompt_attempts):
                try:
                    response = await router.generate(
                        prompt=prompt_payload,
                        provider=provider,
                        model=model,
                        temperature=float(request.get("cover_prompt_temperature") or 0.35),
                        max_tokens=900,
                        task="kwork_cover_prompt",
                        system_prompt=system_prompt,
                        allow_fallback=not bool(provider),
                    )
                    request["_cover_prompt_context"]["prompt_writer_attempts"] = attempt + 1
                    break
                except Exception as exc:
                    last_prompt_error = exc
                    if attempt + 1 >= prompt_attempts:
                        raise
                    await asyncio.sleep(0.25)
            if not response and last_prompt_error is not None:
                raise last_prompt_error
            prompt = self._clean_cover_prompt(response)
            if prompt:
                context = request.get("_cover_prompt_context")
                source = str(context.get("prompt_writer_route") if isinstance(context, dict) else "") or "llm"
                return prompt, source
            request["_cover_prompt_context"] = _cover_prompt_context(
                visual_analysis,
                cover_history,
                route="fallback_empty_prompt",
            )
            return fallback, "fallback"
        except Exception as exc:
            logger.warning(f"KworkAutopublish: cover prompt generation fallback: {exc}")
            request["_cover_prompt_context"] = _cover_prompt_context(
                visual_analysis,
                cover_history,
                route="fallback_exception",
                warning=f"{type(exc).__name__}: {exc}",
            )
            return fallback, "fallback"

    async def analyze_competitor_covers(self, draft: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        if request.get("use_competitor_image_analysis") is False:
            return {"status": "disabled", "brief": "", "images_seen": 0, "image_urls": []}

        items = _competitor_cover_items(request)
        if not items:
            return {"status": "no_images", "brief": "", "images_seen": 0, "image_urls": []}

        downloaded = await self._download_competitor_covers(items)
        image_urls = [item["data_url"] for item in downloaded if item.get("data_url")]
        source_urls = [item["image_url"] for item in downloaded if item.get("image_url")]
        request["_cover_competitor_image_data_urls"] = image_urls
        request["_cover_competitor_image_meta"] = [
            {key: item.get(key) for key in ("title", "image_url", "price", "service_size")} for item in downloaded
        ]
        if not image_urls:
            return {
                "status": "download_failed",
                "brief": "",
                "images_seen": 0,
                "image_urls": [item["image_url"] for item in items],
            }

        prompt = json.dumps(
            {
                "task": "Analyze competitor Kwork cover images and produce a concise visual style brief for a better new cover.",
                "service": {
                    "title": draft.get("title"),
                    "description": _truncate(str(draft.get("description") or ""), 700),
                    "category": request.get("category_name"),
                    "classifier": request.get("classifier_name"),
                    "audience": request.get("audience") or draft.get("auditory") or "",
                },
                "competitor_metadata": [
                    {
                        "index": idx + 1,
                        "title": item.get("title"),
                        "price": item.get("price"),
                        "service_size": item.get("service_size"),
                        "image_url": item.get("image_url"),
                    }
                    for idx, item in enumerate(downloaded)
                ],
                "instructions": [
                    "Actually inspect the attached images, not only the URLs.",
                    "Identify repeated marketplace clichés, weak metaphors, fake UI, noisy text, and irrelevant props to avoid.",
                    "Identify which literal deliverable, process, or customer outcome the competitors fail to show clearly.",
                    "Recommend concrete domain-specific visual proof without copying their composition, palette, typography, or props.",
                    "Do not recommend copying any single competitor cover.",
                    "Return plain text only, 100-180 words.",
                ],
            },
            ensure_ascii=False,
        )

        try:
            from src.brain.llm_router import get_llm_router

            router = get_llm_router()
            response = await router.generate_with_images(
                prompt=prompt,
                image_urls=image_urls,
                provider=(
                    request.get("cover_vision_provider")
                    or os.getenv("KWORK_COVER_VISION_PROVIDER")
                    or request.get("cover_prompt_provider")
                    or os.getenv("KWORK_COVER_PROMPT_PROVIDER")
                    or request.get("provider")
                    or None
                ),
                model=request.get("cover_vision_model") or os.getenv("KWORK_COVER_VISION_MODEL") or None,
                temperature=float(request.get("cover_vision_temperature") or 0.25),
                max_tokens=int(
                    request.get("cover_vision_max_tokens") or os.getenv("KWORK_COVER_VISION_MAX_TOKENS", "700")
                ),
                task="kwork_cover_vision",
                system_prompt=(
                    "You are a visual art director using marketplace images as negative evidence. "
                    "Be practical and concise. Surface clichés to avoid and missing real-world proof. "
                    "Never tell the image model to copy or stylistically match a competitor."
                ),
            )
            brief = self._clean_cover_prompt(response)
            return {
                "status": "analyzed" if brief else "empty",
                "brief": brief,
                "images_seen": len(image_urls),
                "image_urls": source_urls,
            }
        except Exception as exc:
            logger.warning(f"KworkAutopublish: competitor cover vision analysis failed: {exc}")
            return {
                "status": "failed",
                "brief": "",
                "detail": f"{type(exc).__name__}: {exc}",
                "images_seen": len(image_urls),
                "image_urls": source_urls,
            }

    async def _download_competitor_covers(self, items: list[dict[str, str]]) -> list[dict[str, str]]:
        headers = {
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "User-Agent": "Mozilla/5.0 PSR-KworkCoverVision/1.0",
        }
        semaphore = asyncio.Semaphore(4)
        async with httpx.AsyncClient(
            timeout=_vision_timeout_seconds(), follow_redirects=True, trust_env=False
        ) as client:

            async def download(item: dict[str, str]) -> dict[str, str] | None:
                try:
                    async with semaphore:
                        response = await client.get(item["image_url"], headers=headers)
                        response.raise_for_status()
                    raw = response.content[: 5 * 1024 * 1024]
                    if raw:
                        return {
                            **item,
                            "data_url": _image_bytes_to_data_url(raw, response.headers.get("content-type", "")),
                        }
                except Exception as exc:
                    logger.debug(f"KworkAutopublish: competitor cover download failed: {item.get('image_url')} ({exc})")
                return None

            results = await asyncio.gather(*(download(item) for item in items))
        return [item for item in results if item is not None]

    def _cover_prompt_brief(
        self,
        draft: dict[str, Any],
        request: dict[str, Any],
        visual_analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        market_context = request.get("market_context") if isinstance(request.get("market_context"), dict) else {}
        competitors = market_context.get("competitors") or market_context.get("practice_context") or []
        if not isinstance(competitors, list):
            competitors = []
        competitor_examples: list[dict[str, Any]] = []
        for item in competitors[:6]:
            if not isinstance(item, dict):
                continue
            competitor_examples.append(
                {
                    "title": item.get("title"),
                    "image_url": item.get("image_url"),
                    "portfolio_images": (item.get("portfolio_images") or [])[:4]
                    if isinstance(item.get("portfolio_images"), list)
                    else [],
                    "description": _truncate(str(item.get("description") or ""), 450),
                    "service_size": item.get("service_size"),
                    "price": item.get("price"),
                }
            )

        strategy = request.get("_cover_visual_strategy")
        if not isinstance(strategy, dict):
            strategy = _visual_strategy(draft, request)
        requirements = [
            "3:2 cover composition for a freelance marketplace listing",
            "one coherent edge-to-edge scene with one primary focal point and a clear thumbnail silhouette",
            "show literal visual proof of the exact service through its real deliverable, authentic process, or real-world outcome",
            "follow the routed cross-domain visual strategy instead of defaulting to web, software, or graphic design",
            "treat competitor covers as negative evidence: avoid their clichés and never imitate their layout, palette, typography, or props",
            "use recent same-category images only to avoid accidental repetition; do not invent novelty props",
            "no poster, presentation board, case-study slide, moodboard, contact sheet, split screen, before-and-after, multi-panel collage, device stack, floating screens, decorative frame, or card-within-a-card",
            "no abstract geometry, floating cubes, glowing blobs, gradient ribbons, generic icons, fake random interfaces, logos, brand names, contacts, or watermarks",
            "no email address, URL, domain, phone number, @handle, QR code, or contact detail anywhere, including inside the depicted deliverable",
            "plausible anatomy, perspective, scale, materials, lighting, shadows, and interface structure",
            _scene_subject_constraints(strategy),
            f"domain-specific exclusions: {strategy.get('avoid') or ''}",
        ]
        if strategy.get("allow_embedded_text"):
            requirements.extend(
                [
                    "text may appear only where it naturally belongs inside the depicted deliverable",
                    "never add a cover headline, caption, title, subtitle, label, or promotional typography",
                    "never render an email address, URL, domain, phone number, @handle, QR code, or contact detail",
                ]
            )
        else:
            requirements.extend(
                [
                    "do not render readable words, letters, numbers, labels, or signage",
                    "do not reserve space for a later text overlay; use the full canvas for the visual",
                ]
            )

        return {
            "task": "Write a smart prompt for generating a Kwork cover image.",
            "service": {
                "title": draft.get("title"),
                "description": _truncate(str(draft.get("description") or ""), 900),
                "service_summary": request.get("service_summary") or request.get("brief") or "",
                "audience": request.get("audience") or draft.get("auditory") or "",
                "category": request.get("category_name"),
                "classifier": request.get("classifier_name"),
            },
            "variant": {
                "index": int(request.get("variant_index") or 0) + 1,
                "count": max(1, min(3, int(request.get("variant_count") or 1))),
                "creative_direction": request.get("creative_direction") or "",
            },
            "visual_strategy": strategy,
            "competitor_cover_examples": competitor_examples,
            "competitor_negative_evidence": visual_analysis or {"status": "not_run", "brief": ""},
            "recent_generated_cover_history": request.get("_cover_history") or [],
            "selected_market_slice": {
                "kworks_count": market_context.get("kworks_count"),
                "filter_scope": market_context.get("filter_scope"),
                "filter_requests": market_context.get("filter_requests"),
                "demand": market_context.get("demand"),
                "price_steps": market_context.get("price_steps"),
            },
            "attached_image_context": {
                "competitor_covers": len(request.get("_cover_competitor_image_data_urls") or []),
                "recent_generated_covers": len(
                    [item for item in (request.get("_cover_history") or []) if item.get("path")]
                ),
            },
            "requirements": requirements,
        }

    def _fallback_cover_prompt(
        self,
        draft: dict[str, Any],
        request: dict[str, Any],
        visual_analysis: dict[str, Any] | None = None,
    ) -> str:
        strategy = request.get("_cover_visual_strategy")
        if not isinstance(strategy, dict):
            strategy = _visual_strategy(draft, request)
        analysis_brief = str((visual_analysis or {}).get("brief") or "").strip()
        analysis_line = f"Marketplace clichés and missing proof to avoid: {analysis_brief}. " if analysis_brief else ""
        history = request.get("_cover_history") if isinstance(request.get("_cover_history"), list) else []
        history_titles = [
            _truncate(str(item.get("title") or ""), 120)
            for item in history[:3]
            if isinstance(item, dict) and item.get("title")
        ]
        history_line = (
            f"Recent cover titles to avoid repeating too literally: {' | '.join(history_titles)}. "
            if history_titles
            else ""
        )
        direction = str(request.get("creative_direction") or "").strip()
        direction_line = f"Creative direction for this variant: {direction}. " if direction else ""
        text_policy = (
            "Text may appear only where it naturally belongs inside the depicted deliverable; never add a cover headline, caption, label, promotional typography, email address, URL, domain, phone number, @handle, QR code, or contact detail anywhere. "
            if strategy.get("allow_embedded_text")
            else "Do not render readable words, letters, numbers, labels, or signage. "
        )
        return (
            "Create one production-quality 3:2 landscape visual for a freelance marketplace listing. "
            "One coherent edge-to-edge scene, one primary focal point, clear at thumbnail size. "
            "No poster, presentation board, case-study slide, moodboard, contact sheet, split screen, before-and-after, multi-panel collage, device stack, floating screens, decorative frame, or card-within-a-card. "
            "No abstract geometric composition, floating cubes, glowing blobs, gradient ribbons, generic symbolism, logos, brand names, contacts, or watermarks. "
            f"{text_policy}"
            f"Service title: {draft.get('title')}. "
            f"Service description: {_truncate(str(draft.get('description') or ''), 650)}. "
            f"Audience: {request.get('audience') or draft.get('auditory') or ''}. "
            f"Category: {request.get('category_name') or ''}. "
            f"Domain: {strategy.get('domain')}. Scene mode: {strategy.get('scene_mode')}. "
            f"Visual proof: {strategy.get('visual_proof')}. Art direction: {strategy.get('style')}. "
            f"Mandatory subject constraint: {_scene_subject_constraints(strategy)} "
            f"Domain-specific exclusions: {strategy.get('avoid')}. "
            "Use plausible anatomy, perspective, scale, materials, lighting, shadows, and interface structure. "
            "For web or software outside artifact mode, show one convincing already-shipped interface or a real developer using the specific product, never simplified placeholder blocks or generic tech metaphors. "
            f"{analysis_line}"
            f"{history_line}"
            f"{direction_line}"
            f"Context: {request.get('image_context') or request.get('service_summary') or ''}"
        )

    @staticmethod
    def _clean_cover_prompt(value: Any) -> str:
        text = str(value or "").strip()
        text = re.sub(r"^```(?:text|json)?", "", text, flags=re.I).strip()
        text = re.sub(r"```$", "", text).strip()
        if text.startswith("{"):
            parsed = _extract_json_object(text)
            if parsed:
                text = str(parsed.get("prompt") or parsed.get("image_prompt") or "")
        text = " ".join(text.split())
        return _truncate(text, 5000)

    def _fallback_draft(self, request: dict[str, Any]) -> dict[str, Any]:
        service = str(request.get("service_summary") or request.get("brief") or "автоматизацию под задачу")
        category = str(request.get("category_name") or "выбранной категории")
        title = _truncate(f"Сделаю {service}", 80)
        description = (
            f"Разработаю {service} для вашей задачи в категории {category}. "
            "Перед началом уточню цель, входные данные, ожидаемый результат и ограничения. "
            "После согласования подготовлю рабочее решение, проверю основные сценарии и передам понятные инструкции. "
            "Если у вас уже есть исходные материалы, референс или техническое задание, используем их как контекст для более точного результата."
        )
        return {
            "title": title,
            "description": description,
            "instruction": "Опишите задачу, приложите примеры, доступы без паролей или тестовые данные, сроки и желаемый формат результата.",
            "auditory": str(request.get("audience") or ""),
            "service_size": "1 задача",
            "volume": "1 готовое решение",
            "price": int(request.get("price") or 500),
            "work_time": int(request.get("work_time") or 3),
            "faq": [
                {
                    "question": "Что нужно для старта?",
                    "answer": "Краткое описание задачи, примеры и критерии готовности.",
                },
                {
                    "question": "Можно ли доработать после проверки?",
                    "answer": "Да, в рамках согласованной задачи внесу правки по результатам проверки.",
                },
            ],
        }

    def _normalize_draft(self, parsed: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
        faq = parsed.get("faq")
        if not isinstance(faq, list):
            faq = fallback["faq"]
        normalized_faq = []
        for item in faq[:5]:
            if not isinstance(item, dict):
                continue
            normalized_faq.append(
                {
                    "question": _sanitize_kwork_text(item.get("question")),
                    "answer": _sanitize_kwork_text(item.get("answer")),
                }
            )
        normalized = {
            "title": _truncate(_sanitize_kwork_text(parsed.get("title") or fallback["title"]), 80),
            "description": _sanitize_kwork_text(parsed.get("description") or fallback["description"]),
            "instruction": _sanitize_kwork_text(parsed.get("instruction") or fallback["instruction"]),
            "auditory": _sanitize_kwork_text(parsed.get("auditory") or fallback["auditory"]),
            "service_size": _sanitize_kwork_text(parsed.get("service_size") or fallback["service_size"]),
            "volume": _sanitize_kwork_text(parsed.get("volume") or fallback["volume"]),
            "price": int(parsed.get("price") or fallback["price"]),
            "work_time": int(parsed.get("work_time") or fallback["work_time"]),
            "faq": normalized_faq,
        }
        return normalized

    async def _generate_image_file(self, path: Path, prompt: str, *, purpose: str) -> dict[str, Any]:
        from src.action.proposal_image import ProposalImageGenerator, _first_openai_key, _openai_url

        settings = _image_settings(purpose)
        fallback_url = _openai_url("images/generations")
        endpoint = _image_api_url("images/generations", fallback_url)
        api_key = _image_api_key(_first_openai_key())
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY/OPENAI_API_KEYS is not configured")

        payload = {**settings, "prompt": prompt}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        started = time.perf_counter()
        errors: list[str] = []
        response: httpx.Response | None = None
        async with httpx.AsyncClient(timeout=_image_timeout_seconds(), trust_env=False) as client:
            for attempt in range(2):
                try:
                    response = await client.post(endpoint, headers=headers, json=payload)
                except httpx.RequestError as exc:
                    errors.append(f"attempt {attempt + 1}: {type(exc).__name__}: {exc}")
                    if attempt == 0:
                        await asyncio.sleep(1.0)
                        continue
                    raise RuntimeError("; ".join(errors)) from exc
                if response.status_code in {429, 500, 502, 503, 504} and attempt == 0:
                    errors.append(f"attempt 1: HTTP {response.status_code} {response.text[:240]}")
                    await asyncio.sleep(1.0)
                    continue
                break

        if response is None:
            raise RuntimeError("image API returned no response")
        if response.status_code >= 400:
            raise RuntimeError(
                f"{settings['model']} image API returned HTTP {response.status_code}: {response.text[:500]}"
            )
        data = response.json()
        served_model = str(data.get("model") or "").strip() if isinstance(data, dict) else ""
        if served_model and served_model != KWORK_IMAGE_MODEL:
            raise RuntimeError(
                f"image API model mismatch: requested {KWORK_IMAGE_MODEL}, response reported {served_model}"
            )
        b64 = ProposalImageGenerator._extract_b64_image(data)
        if not b64:
            raise RuntimeError("image API returned no base64 image")
        path = ensure_parent(path)
        path.write_bytes(base64.b64decode(b64))
        normalized, normalize_detail = _normalize_cover_png(path)
        if not normalized:
            path.unlink(missing_ok=True)
            raise RuntimeError(f"generated image validation failed: {normalize_detail}")

        return {
            "requested_model": settings["model"],
            "served_model": served_model,
            "model_verification": "response_reported" if served_model else "request_only",
            "size": settings["size"],
            "quality": settings["quality"],
            "endpoint": endpoint,
            "request_id": response.headers.get("x-request-id") or response.headers.get("request-id") or "",
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "attempts": 1 + len(errors),
            "retry_errors": errors,
            "visual_pipeline_version": KWORK_VISUAL_PIPELINE_VERSION,
        }

    async def validate_generated_cover(
        self,
        path: Path,
        draft: dict[str, Any],
        request: dict[str, Any],
        *,
        purpose: str = "cover",
        expected_prompt: str = "",
    ) -> dict[str, Any]:
        if not _cover_qa_enabled():
            return {"status": "disabled", "ok": True, "score": None, "issues": []}
        image_url = _image_file_to_data_url(path)
        if not image_url:
            return {"status": "failed", "ok": False, "score": 0, "issues": ["generated image is unreadable"]}

        strategy = request.get("_cover_visual_strategy")
        if not isinstance(strategy, dict):
            strategy = _visual_strategy(draft, request)
        prompt = json.dumps(
            {
                "task": f"Quality-gate this generated marketplace {purpose} image from pixels.",
                "service": {
                    "title": draft.get("title"),
                    "description": _truncate(str(draft.get("description") or ""), 650),
                    "category": request.get("category_name") or draft.get("category_name"),
                    "classifier": request.get("classifier_name") or draft.get("classifier_name"),
                },
                "visual_strategy": strategy,
                "subject_constraint": _scene_subject_constraints(strategy),
                "expected_visual": _truncate(expected_prompt, 900),
                "interface_layout_policy": (
                    "A single coherent product interface may contain native sections, rows, cards, navigation, and embedded UI. "
                    "Do not mistake normal internal product layout for a collage or case-study board."
                ),
                "reject_when": [
                    "the exact service is not recognizable through a concrete deliverable, authentic process, or real outcome",
                    "the image is mainly abstract geometry, generic symbolism, decorative gradients, or unrelated stock imagery",
                    "the image is a poster, presentation board, case-study slide, moodboard, contact sheet, split screen, before-and-after, disconnected multi-panel collage, device stack, or floating-screen composition rather than one coherent deliverable",
                    "a promotional headline, caption, title banner, label, lower-third gradient, logo, watermark, contact, or brand name was added over the visual",
                    "any email address, URL, domain, phone number, @handle, QR code, or contact detail appears anywhere, even as embedded interface or document content",
                    "the main subject is unclear at thumbnail size or important content is cropped",
                    "anatomy, perspective, scale, material, lighting, shadows, or interface structure are visibly broken",
                    "embedded product or interface text is random or broken when text is naturally required by the deliverable",
                    *(
                        [
                            "web/software artifact mode contains any person, face, hand, monitor, laptop, phone, desk, room, keyboard, books, coffee, plants, or lifestyle props",
                            "web/software artifact mode photographs a device instead of showing the digital product itself as a direct full-canvas interface",
                        ]
                        if strategy.get("domain") == "web_software"
                        and strategy.get("scene_mode") == "artifact"
                        else []
                    ),
                ],
                "text_policy": (
                    "Natural text inside the actual depicted deliverable is allowed, but external cover typography is not."
                    if strategy.get("allow_embedded_text")
                    else "Any readable text is a defect."
                ),
                "required_json": {
                    "ok": "boolean",
                    "score": "number 0-10",
                    "issues": ["short concrete issue"],
                    "correction": "one concise prompt correction",
                },
            },
            ensure_ascii=False,
        )
        try:
            from src.action.proposal_image import _first_openai_key, _openai_url

            instructions = (
                "You are a strict visual QA reviewer. Inspect pixels, not filenames or intent. "
                "Return one valid JSON object only. Do not be lenient with generic, abstract, collage, or poster-like results."
            )
            qa_provider = str(os.getenv("KWORK_COVER_QA_PROVIDER") or "auto").strip().lower()
            if qa_provider == "auto":
                if _openai_api_keys():
                    qa_provider = "openai"
                elif os.getenv("GROQ_API_KEY", "").strip():
                    qa_provider = "groq"
                else:
                    qa_provider = "image_gateway"
            if qa_provider == "openai":
                api_keys = _openai_api_keys()
                if not api_keys:
                    raise RuntimeError("OPENAI_API_KEY/OPENAI_API_KEYS is not configured for cover QA")
                base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com").strip().rstrip("/")
                prefix = os.getenv("OPENAI_API_PREFIX", "/v1").strip("/")
                if prefix and not base.endswith(f"/{prefix}"):
                    base = f"{base}/{prefix}"
                endpoint = f"{base}/responses"
                qa_model = str(os.getenv("KWORK_COVER_QA_MODEL") or "gpt-5.6-terra").strip()
                payload = {
                    "model": qa_model,
                    "instructions": instructions,
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": prompt},
                                {"type": "input_image", "image_url": image_url, "detail": "high"},
                            ],
                        }
                    ],
                    "reasoning": {"effort": "low"},
                    "max_output_tokens": 650,
                    "store": False,
                }
            elif qa_provider == "groq":
                groq_key = os.getenv("GROQ_API_KEY", "").strip()
                if not groq_key:
                    raise RuntimeError("GROQ_API_KEY is not configured for cover QA")
                api_keys = [groq_key]
                endpoint = "https://api.groq.com/openai/v1/chat/completions"
                qa_model = str(
                    os.getenv("KWORK_COVER_QA_MODEL") or "meta-llama/llama-4-scout-17b-16e-instruct"
                ).strip()
                payload = {
                    "model": qa_model,
                    "messages": [
                        {"role": "system", "content": instructions},
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": image_url}},
                            ],
                        },
                    ],
                    "temperature": 0,
                    "max_tokens": 650,
                    "response_format": {"type": "json_object"},
                }
            elif qa_provider == "image_gateway":
                image_gateway_key = _image_api_key(_first_openai_key())
                if not image_gateway_key:
                    raise RuntimeError("image gateway API key is not configured for cover QA")
                api_keys = [image_gateway_key]
                endpoint = _image_api_url("responses", _openai_url("images/generations"))
                qa_model = str(os.getenv("KWORK_COVER_QA_MODEL") or "gpt-5.4").strip()
                payload = {
                    "model": qa_model,
                    "instructions": instructions,
                    "input": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": prompt},
                                {"type": "input_image", "image_url": image_url, "detail": "high"},
                            ],
                        }
                    ],
                    "max_output_tokens": 650,
                    "store": False,
                }
            else:
                raise RuntimeError(f"unsupported KWORK_COVER_QA_PROVIDER={qa_provider!r}")
            qa_response: httpx.Response | None = None
            async with httpx.AsyncClient(timeout=_image_timeout_seconds(), trust_env=False) as client:
                for index, api_key in enumerate(api_keys):
                    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
                    qa_response = await client.post(endpoint, headers=headers, json=payload)
                    if qa_response.status_code not in {401, 402, 403, 429} or index + 1 >= len(api_keys):
                        break
            if qa_response is None:
                raise RuntimeError("cover QA API returned no response")
            if qa_response.status_code >= 400:
                raise RuntimeError(
                    f"cover QA API returned HTTP {qa_response.status_code}: {qa_response.text[:500]}"
                )
            qa_data = qa_response.json()
            if qa_provider == "groq":
                choices = qa_data.get("choices") if isinstance(qa_data, dict) else None
                first_choice = choices[0] if isinstance(choices, list) and choices else {}
                message = first_choice.get("message") if isinstance(first_choice, dict) else {}
                response_text = str(message.get("content") or "") if isinstance(message, dict) else ""
            else:
                response_text = _responses_output_text(qa_data)
            if not response_text:
                raise RuntimeError("cover QA API returned no output text")
            parsed = _extract_json_object(response_text) or {}
            raw_ok = parsed.get("ok")
            raw_score = parsed.get("score")
            raw_issues = parsed.get("issues")
            if not isinstance(raw_ok, bool):
                raise ValueError("cover QA response field 'ok' must be boolean")
            if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
                raise ValueError("cover QA response field 'score' must be numeric")
            score = float(raw_score)
            if not 0 <= score <= 10:
                raise ValueError("cover QA response field 'score' must be between 0 and 10")
            if not isinstance(raw_issues, list):
                raise ValueError("cover QA response field 'issues' must be an array")
            issues = [str(item)[:240] for item in raw_issues[:8]]
            ok = raw_ok is True and score >= _cover_qa_min_score() and not issues
            return {
                "status": "passed" if ok else "rejected",
                "ok": ok,
                "score": score,
                "issues": issues,
                "correction": _truncate(str(parsed.get("correction") or ""), 600),
                "review_model_requested": qa_model,
                "review_model_served": str(qa_data.get("model") or "") if isinstance(qa_data, dict) else "",
                "review_provider": qa_provider,
                "request_id": qa_response.headers.get("x-request-id")
                or qa_response.headers.get("request-id")
                or "",
            }
        except Exception as exc:
            logger.warning(f"KworkAutopublish: cover quality gate unavailable: {exc}")
            return {
                "status": "unavailable",
                "ok": False,
                "score": None,
                "issues": ["visual QA provider is unavailable"],
                "detail": f"{type(exc).__name__}: {exc}",
            }

    async def generate_cover(self, draft: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        prompt, prompt_source = await self.build_cover_prompt(draft, request)
        title_slug = _slug(str(draft.get("title") or "kwork-cover"))
        image_suffix = hashlib.sha1(f"{prompt}|{time.time_ns()}".encode("utf-8", errors="ignore")).hexdigest()[:10]
        path = PROPOSAL_ASSETS_DIR / "kwork_autopublish" / f"{title_slug}-{image_suffix}.png"
        request["_cover_generation_prompt"] = prompt

        try:
            image_settings = await self._generate_image_file(path, prompt, purpose="cover")
        except Exception as exc:
            logger.warning(f"KworkAutopublish: strict {KWORK_IMAGE_MODEL} cover generation failed: {exc}")
            configured_model = os.getenv("KWORK_COVER_IMAGE_MODEL", KWORK_IMAGE_MODEL).strip() or KWORK_IMAGE_MODEL
            request["_cover_image_settings"] = {
                "requested_model": configured_model,
                "required_model": KWORK_IMAGE_MODEL,
                "status": "error",
            }
            return self._with_visual_analysis(
                {
                    "status": "error",
                    "code": "gpt_image_2_generation_failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "prompt": prompt,
                    "prompt_source": prompt_source,
                    "requested_image_model": configured_model,
                    "required_image_model": KWORK_IMAGE_MODEL,
                },
                request,
            )

        quality_gate = await self.validate_generated_cover(path, draft, request)
        if not quality_gate.get("ok"):
            if quality_gate.get("status") != "rejected":
                path.unlink(missing_ok=True)
                request["_cover_quality_gate"] = quality_gate
                request["_cover_image_settings"] = image_settings
                return self._with_visual_analysis(
                    {
                        "status": "error",
                        "code": "cover_quality_gate_unavailable",
                        "detail": quality_gate.get("detail")
                        or "; ".join(str(item) for item in quality_gate.get("issues") or [])
                        or "Visual QA did not return a valid verdict.",
                        "prompt": prompt,
                        "prompt_source": prompt_source,
                        "requested_image_model": KWORK_IMAGE_MODEL,
                        "quality_gate": quality_gate,
                    },
                    request,
                )
            correction = str(quality_gate.get("correction") or "").strip()
            if not correction:
                correction = "; ".join(str(item) for item in quality_gate.get("issues") or [])
            retry_prompt = (
                f"{prompt} Mandatory correction after visual QA: {correction or 'show literal service proof in one coherent scene'}. "
                "Preserve every no-text, no-collage, no-poster, no-abstract, and no-watermark constraint."
            )
            try:
                retry_settings = await self._generate_image_file(path, retry_prompt, purpose="cover")
            except Exception as exc:
                path.unlink(missing_ok=True)
                request["_cover_quality_gate"] = quality_gate
                request["_cover_image_settings"] = image_settings
                return self._with_visual_analysis(
                    {
                        "status": "error",
                        "code": "cover_quality_retry_failed",
                        "detail": f"{type(exc).__name__}: {exc}",
                        "prompt": retry_prompt,
                        "prompt_source": f"{prompt_source}+qa_retry",
                        "requested_image_model": KWORK_IMAGE_MODEL,
                        "quality_gate": quality_gate,
                    },
                    request,
                )
            retry_gate = await self.validate_generated_cover(path, draft, request)
            retry_settings["qa_retry"] = True
            retry_settings["initial_quality_gate"] = quality_gate
            image_settings = retry_settings
            quality_gate = retry_gate
            prompt = retry_prompt
            prompt_source = f"{prompt_source}+qa_retry"
            request["_cover_generation_prompt"] = prompt
            if not quality_gate.get("ok"):
                path.unlink(missing_ok=True)
                request["_cover_quality_gate"] = quality_gate
                request["_cover_image_settings"] = image_settings
                return self._with_visual_analysis(
                    {
                        "status": "error",
                        "code": "cover_quality_gate_failed",
                        "detail": "Generated image was rejected twice by visual QA.",
                        "prompt": prompt,
                        "prompt_source": prompt_source,
                        "requested_image_model": KWORK_IMAGE_MODEL,
                        "quality_gate": quality_gate,
                    },
                    request,
                )

        request["_cover_quality_gate"] = quality_gate
        request["_cover_image_settings"] = image_settings

        self._write_cover_sidecar(
            path,
            draft,
            request,
            prompt=prompt,
            prompt_source=prompt_source,
            status="generated",
        )
        return self._with_visual_analysis(
            self.image_result(
                "generated",
                path,
                prompt=prompt,
                prompt_source=prompt_source,
                image_settings=image_settings,
            ),
            request,
        )

    async def generate_portfolio_assets(
        self,
        draft: dict[str, Any],
        request: dict[str, Any],
        *,
        cover_prompt: str = "",
    ) -> list[dict[str, Any]]:
        count = _required_portfolio_count(draft)
        if count <= 0:
            return []
        reusable = self.ensure_portfolio_assets(draft)
        if len(reusable) >= count:
            return reusable[:count]

        specs = _portfolio_image_specs(draft, request, cover_prompt=cover_prompt, count=count)
        output_dir = PROPOSAL_ASSETS_DIR / "kwork_autopublish"
        stem = _slug(str(draft.get("title") or int(time.time())))
        try:
            concurrency = max(1, min(3, int(os.getenv("KWORK_PORTFOLIO_IMAGE_CONCURRENCY", "2"))))
        except (TypeError, ValueError):
            concurrency = 2
        semaphore = asyncio.Semaphore(concurrency)

        async def generate_one(index: int, spec: dict[str, Any]) -> dict[str, Any]:
            prompt = spec["prompt"]
            portfolio_strategy = (
                spec.get("visual_strategy")
                if isinstance(spec.get("visual_strategy"), dict)
                else _visual_strategy(draft, {**request, "scene_mode": spec.get("scene_mode") or "artifact"})
            )
            qa_request = {**request, "_cover_visual_strategy": portfolio_strategy}
            suffix = hashlib.sha1(f"{prompt}|{time.time_ns()}|{index}".encode()).hexdigest()[:10]
            path = output_dir / f"{stem}-{suffix}-portfolio-{index + 1:02d}.png"
            async with semaphore:
                image_settings = await self._generate_image_file(path, prompt, purpose="portfolio")
                quality_gate = await self.validate_generated_cover(
                    path,
                    draft,
                    qa_request,
                    purpose="portfolio",
                    expected_prompt=prompt,
                )
                if not quality_gate.get("ok"):
                    if quality_gate.get("status") != "rejected":
                        path.unlink(missing_ok=True)
                        raise RuntimeError(
                            f"portfolio visual {index + 1} QA unavailable: "
                            f"{quality_gate.get('detail') or quality_gate.get('issues') or 'invalid verdict'}"
                        )
                    correction = str(quality_gate.get("correction") or "").strip()
                    if not correction:
                        correction = "; ".join(str(item) for item in quality_gate.get("issues") or [])
                    retry_prompt = (
                        f"{prompt} Mandatory correction after visual QA: "
                        f"{correction or 'show the exact portfolio proof in one coherent scene'}. "
                        "Preserve every no-text, no-collage, no-poster, no-abstract, and no-watermark constraint."
                    )
                    retry_settings = await self._generate_image_file(path, retry_prompt, purpose="portfolio")
                    retry_gate = await self.validate_generated_cover(
                        path,
                        draft,
                        qa_request,
                        purpose="portfolio",
                        expected_prompt=retry_prompt,
                    )
                    retry_settings["qa_retry"] = True
                    retry_settings["initial_quality_gate"] = quality_gate
                    image_settings = retry_settings
                    quality_gate = retry_gate
                    prompt = retry_prompt
                    if not quality_gate.get("ok"):
                        path.unlink(missing_ok=True)
                        raise RuntimeError(
                            f"portfolio visual {index + 1} was rejected twice by QA: "
                            f"{quality_gate.get('issues') or quality_gate.get('detail') or 'unknown defect'}"
                        )
            asset = {
                "title": spec["title"],
                "subtitle": spec["subtitle"],
                "path": str(path),
                "asset_url": f"{API_BASE_URL}/api/kwork/autopublish/assets/{path.name}",
                "prompt": prompt,
                "image_generation": image_settings,
                "quality_gate": quality_gate,
                "visual_strategy": portfolio_strategy,
                "visual_pipeline_version": KWORK_VISUAL_PIPELINE_VERSION,
            }
            metadata = {
                "asset_kind": "portfolio",
                "visual_pipeline_version": KWORK_VISUAL_PIPELINE_VERSION,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "status": "generated",
                "path": str(path),
                "title": spec["title"],
                "subtitle": spec["subtitle"],
                "service_title": draft.get("title"),
                "category": request.get("category_name") or draft.get("category_name"),
                "classifier": request.get("classifier_name") or draft.get("classifier_name"),
                "visual_strategy": portfolio_strategy,
                "prompt": prompt,
                "image_generation": image_settings,
                "quality_gate": quality_gate,
            }
            try:
                path.with_suffix(".portfolio.json").write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception as exc:
                logger.debug(f"KworkAutopublish: portfolio sidecar write failed: {exc}")
            return asset

        generated = await asyncio.gather(
            *(generate_one(index, spec) for index, spec in enumerate(specs)),
            return_exceptions=True,
        )
        assets: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for index, item in enumerate(generated):
            if isinstance(item, Exception):
                errors.append({"index": index + 1, "detail": f"{type(item).__name__}: {item}"})
            else:
                assets.append(item)
        if errors:
            request["_portfolio_generation_errors"] = errors
            logger.warning(f"KworkAutopublish: {KWORK_IMAGE_MODEL} portfolio generation incomplete: {errors}")
        return assets

    @staticmethod
    def _with_visual_analysis(result: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        analysis = request.get("_cover_visual_analysis")
        if isinstance(analysis, dict):
            result["visual_analysis_status"] = analysis.get("status") or ""
            result["competitor_images_seen"] = int(analysis.get("images_seen") or 0)
            if analysis.get("brief"):
                result["visual_style_brief"] = analysis["brief"]
            if analysis.get("detail"):
                result["visual_analysis_detail"] = analysis["detail"]
        context = request.get("_cover_prompt_context")
        if isinstance(context, dict):
            result["cover_prompt_context"] = context
        strategy = request.get("_cover_visual_strategy")
        if isinstance(strategy, dict):
            result["visual_strategy"] = strategy
        image_settings = request.get("_cover_image_settings")
        if isinstance(image_settings, dict):
            result["image_generation"] = image_settings
        quality_gate = request.get("_cover_quality_gate")
        if isinstance(quality_gate, dict):
            result["quality_gate"] = quality_gate
        return result

    @staticmethod
    def image_result(
        status: str,
        path: Path,
        *,
        prompt: str = "",
        detail: str = "",
        prompt_source: str = "",
        image_settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        filename = path.name
        result = {
            "status": status,
            "path": str(path),
            "filename": filename,
            "asset_url": f"{API_BASE_URL}/api/kwork/autopublish/assets/{filename}",
            "sidecar_path": str(_cover_sidecar_path(path)),
        }
        if prompt:
            result["prompt"] = prompt
        if prompt_source:
            result["prompt_source"] = prompt_source
        if image_settings:
            result["requested_image_model"] = image_settings.get("requested_model") or ""
            result["served_image_model"] = image_settings.get("served_model") or ""
            result["image_model_verification"] = image_settings.get("model_verification") or ""
            result["image_size"] = image_settings.get("size") or ""
            result["image_quality"] = image_settings.get("quality") or ""
            result["image_request_id"] = image_settings.get("request_id") or ""
            result["image_latency_ms"] = image_settings.get("latency_ms") or 0
            result["visual_pipeline_version"] = image_settings.get("visual_pipeline_version") or ""
        if detail:
            result["detail"] = detail
        return result

    @staticmethod
    def build_form_payload(draft: dict[str, Any]) -> list[tuple[str, str]]:
        """Build a form-compatible payload preserving repeated checkbox names."""
        faq = draft.get("faq") if isinstance(draft.get("faq"), list) else []
        selection, _ = _normalized_draft_selection(draft)

        pairs: list[tuple[str, str]] = [
            ("lang", str(draft.get("lang") or "ru")),
            ("title", str(draft.get("title") or "")),
            ("category_id", str(draft.get("category_id") or "")),
            ("description", str(draft.get("description") or "")),
            ("auditory", str(draft.get("auditory") or "")),
            ("instruction", str(draft.get("instruction") or "")),
            ("service_size", str(draft.get("service_size") or "1 задача")),
            ("volume", str(draft.get("volume") or "1 готовое решение")),
            ("work_time", str(draft.get("work_time") or 3)),
            ("min_volume_price", str(draft.get("price") or 500)),
            ("is_save_kwork", "1"),
        ]

        if int(draft.get("category_id") or 0) == 25:
            pairs.extend(
                [
                    (
                        "bundle_standard_description",
                        str(
                            draft.get("bundle_standard_description")
                            or "Адаптация одного готового логотипа под кириллицу"
                        ),
                    ),
                    ("package_volume", str(draft.get("package_volume") or 1)),
                    (
                        "bundle_standard_duration",
                        str(draft.get("bundle_standard_duration") or draft.get("work_time") or 3),
                    ),
                    ("bundle_extra_standard_value[category][190]", "1"),
                    ("bundle_extra_standard_value[category][191]", str(draft.get("package_volume") or 1)),
                ]
            )

        for key in ("csrftoken", "draft_id"):
            value = draft.get(key)
            if value not in (None, ""):
                pairs.append((key, str(value)))

        explicit_names = {name for name, _ in pairs}
        hidden_fields = draft.get("hidden_fields") if isinstance(draft.get("hidden_fields"), dict) else {}
        for name, value in hidden_fields.items():
            if name in explicit_names or value in (None, ""):
                continue
            pairs.append((str(name), str(value)))
            explicit_names.add(str(name))

        for name, value in selection.items():
            if value in (None, ""):
                continue
            values = value if isinstance(value, list) else [value]
            for item in values:
                if item not in (None, ""):
                    pairs.append((str(name), str(item)))

        for index, item in enumerate(faq):
            if not isinstance(item, dict):
                continue
            question = str(item.get("question") or "").strip()
            answer = str(item.get("answer") or "").strip()
            if question:
                pairs.append((f"faq[{index}][question]", question))
            if answer:
                pairs.append((f"faq[{index}][answer]", answer))

        cover_upload = draft.get("cover_upload") if isinstance(draft.get("cover_upload"), dict) else {}
        first_photo_json = cover_upload.get("first_photo_json") or draft.get("first_photo_json")
        if first_photo_json:
            pairs.append(
                (
                    "first_photo_json",
                    first_photo_json
                    if isinstance(first_photo_json, str)
                    else json.dumps(first_photo_json, ensure_ascii=False),
                )
            )
        first_photo_path = cover_upload.get("first_photo_path") or draft.get("first_photo_path")
        if first_photo_path:
            pairs.append(("first_photo_path", str(first_photo_path)))
            pairs.append(("first-kwork-photo", "null"))
        crop = (
            cover_upload.get("first-kwork-photo-size[]")
            or cover_upload.get("crop")
            or draft.get("first-kwork-photo-size[]")
        )
        if crop:
            pairs.append(
                (
                    "first-kwork-photo-size[]",
                    crop if isinstance(crop, str) else json.dumps(crop, ensure_ascii=False),
                )
            )
        portfolios = draft.get("portfolios") if isinstance(draft.get("portfolios"), list) else []
        if portfolios:
            pairs.append(("portfolio", json.dumps(portfolios, ensure_ascii=False)))
        return pairs

    def build_save_payload(self, draft: dict[str, Any]) -> dict[str, Any]:
        faq = draft.get("faq") if isinstance(draft.get("faq"), list) else []
        selection, normalization = _normalized_draft_selection(draft)
        form_payload = self.build_form_payload(draft)
        return {
            "lang": draft.get("lang") or "ru",
            "title": draft.get("title") or "",
            "category_id": draft.get("category_id"),
            "description": draft.get("description") or "",
            "auditory": draft.get("auditory") or "",
            "instruction": draft.get("instruction") or "",
            "service_size": draft.get("service_size") or "1 задача",
            "volume": draft.get("volume") or "1 готовое решение",
            "work_time": draft.get("work_time") or 3,
            "min_volume_price": draft.get("price") or 500,
            "attributes": selection,
            "attribute_ids": selected_attribute_ids(selection),
            "attribute_manifest": draft.get("attribute_manifest") or {},
            "attribute_manifest_hash": (
                normalization["manifest_hash"] if normalization is not None else draft.get("attribute_manifest_hash")
            ),
            "selection_hash": normalization["selection_hash"]
            if normalization is not None
            else draft.get("selection_hash"),
            "faq": faq,
            "portfolio": draft.get("portfolios") if isinstance(draft.get("portfolios"), list) else [],
            "is_save_kwork": 1,
            "form_payload": form_payload,
        }

    @staticmethod
    def _canonical_for_hash(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): KworkAutopublishService._canonical_for_hash(value[key]) for key in sorted(value)}
        if isinstance(value, list):
            return [KworkAutopublishService._canonical_for_hash(item) for item in value]
        if isinstance(value, tuple):
            return [KworkAutopublishService._canonical_for_hash(item) for item in value]
        return value

    def draft_hash(self, draft: dict[str, Any]) -> str:
        payload = self.build_save_payload(draft)
        canonical = self._canonical_for_hash(payload)
        raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _publish_secret() -> bytes:
        secret = os.getenv("KWORK_AUTOPUBLISH_CONFIRM_SECRET", "").strip() or _PUBLISH_CONFIRM_SECRET
        return secret.encode("utf-8")

    def issue_publish_token(self, draft: dict[str, Any]) -> dict[str, Any]:
        issued_at = int(time.time())
        ttl = int(os.getenv("KWORK_AUTOPUBLISH_CONFIRM_TTL_SECONDS", "600") or "600")
        draft_hash = self.draft_hash(draft)
        message = f"{issued_at}.{draft_hash}"
        signature = hmac.new(self._publish_secret(), message.encode("utf-8"), hashlib.sha256).hexdigest()
        return {
            "token": f"{issued_at}.{draft_hash}.{signature}",
            "draft_hash": draft_hash,
            "expires_at": issued_at + ttl,
            "ttl_seconds": ttl,
            "confirmation_phrase": os.getenv("KWORK_AUTOPUBLISH_CONFIRM_PHRASE", PUBLISH_CONFIRMATION_PHRASE),
        }

    def validate_publish_confirmation(
        self, draft: dict[str, Any], token: str = "", confirmation: str = ""
    ) -> dict[str, Any]:
        phrase = os.getenv("KWORK_AUTOPUBLISH_CONFIRM_PHRASE", PUBLISH_CONFIRMATION_PHRASE)
        if confirmation.strip() != phrase:
            return {"ok": False, "code": "confirmation_phrase_required", "detail": f"Type {phrase} to publish live."}

        parts = str(token or "").split(".")
        if len(parts) != 3:
            return {
                "ok": False,
                "code": "publish_token_required",
                "detail": "Live publish requires a fresh preflight token.",
            }

        issued_raw, token_hash, signature = parts
        try:
            issued_at = int(issued_raw)
        except ValueError:
            return {"ok": False, "code": "invalid_publish_token", "detail": "Publish token timestamp is invalid."}

        ttl = int(os.getenv("KWORK_AUTOPUBLISH_CONFIRM_TTL_SECONDS", "600") or "600")
        if issued_at < int(time.time()) - ttl:
            return {
                "ok": False,
                "code": "publish_token_expired",
                "detail": "Publish token expired; run preflight again.",
            }

        draft_hash = self.draft_hash(draft)
        if not hmac.compare_digest(token_hash, draft_hash):
            return {"ok": False, "code": "publish_token_draft_mismatch", "detail": "Draft changed after preflight."}

        message = f"{issued_at}.{token_hash}"
        expected = hmac.new(self._publish_secret(), message.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return {"ok": False, "code": "invalid_publish_token", "detail": "Publish token signature is invalid."}

        return {"ok": True, "code": "confirmed", "draft_hash": draft_hash, "expires_at": issued_at + ttl}

    @staticmethod
    def live_preflight(draft: dict[str, Any]) -> dict[str, Any]:
        missing: list[str] = []
        selection, normalization = _normalized_draft_selection(draft)
        if not isinstance(selection, dict) or not any(
            value not in (None, "") and not (isinstance(value, list) and len(value) == 0)
            for value in selection.values()
        ):
            missing.append("attribute_selection")

        manifest = draft.get("attribute_manifest") if isinstance(draft.get("attribute_manifest"), dict) else {}
        if not manifest or ("controls" not in manifest and "unresolved_required" not in manifest):
            missing.append("attribute_manifest")
        unresolved = (
            normalization["unresolved_required"]
            if normalization is not None
            else manifest.get("unresolved_required")
            if isinstance(manifest.get("unresolved_required"), list)
            else []
        )
        for item in unresolved:
            if item not in (None, "") and str(item) not in missing:
                missing.append(str(item))
        if normalization is not None:
            if normalization["issues"] or not normalization["valid"]:
                missing.append("attribute_selection_invalid")
            expected_manifest_hash = str(draft.get("attribute_manifest_hash") or "").strip()
            actual_manifest_hash = attribute_manifest_hash(manifest)
            if expected_manifest_hash and expected_manifest_hash != actual_manifest_hash:
                missing.append("attribute_manifest_stale")

        has_cover_upload = isinstance(draft.get("cover_upload"), dict) and bool(
            draft["cover_upload"].get("first_photo_json") or draft["cover_upload"].get("first_photo_path")
        )
        has_cover_path = bool(draft.get("cover_image_path") or draft.get("image_path"))
        if not has_cover_upload and not has_cover_path:
            missing.append("cover")
        cover_image = draft.get("cover_image") if isinstance(draft.get("cover_image"), dict) else None
        if has_cover_path and not has_cover_upload:
            if cover_image is None:
                missing.append("cover_visual_outdated")
            else:
                generation = (
                    cover_image.get("image_generation")
                    if isinstance(cover_image.get("image_generation"), dict)
                    else {}
                )
                quality_gate = (
                    cover_image.get("quality_gate")
                    if isinstance(cover_image.get("quality_gate"), dict)
                    else {}
                )
                requested_model = str(
                    cover_image.get("requested_image_model") or generation.get("requested_model") or ""
                )
                pipeline_version = str(
                    cover_image.get("visual_pipeline_version")
                    or generation.get("visual_pipeline_version")
                    or ""
                )
                qa_status = str(quality_gate.get("status") or "")
                if (
                    requested_model != KWORK_IMAGE_MODEL
                    or pipeline_version != KWORK_VISUAL_PIPELINE_VERSION
                    or (_cover_qa_enabled() and qa_status != "passed")
                ):
                    missing.append("cover_visual_outdated")

        return {
            "ok": not missing,
            "missing": list(dict.fromkeys(missing)),
            "code": "ok" if not missing else "live_preflight_failed",
            "detail": "" if not missing else f"Live publish requires: {', '.join(missing)}.",
            "selection_validation": (
                {
                    "valid": normalization["valid"],
                    "clean": normalization["clean"],
                    "issues": normalization["issues"],
                    "unresolved_required": normalization["unresolved_required"],
                    "manifest_hash": normalization["manifest_hash"],
                    "selection_hash": normalization["selection_hash"],
                }
                if normalization is not None
                else None
            ),
        }

    def publish_preflight(self, draft: dict[str, Any]) -> dict[str, Any]:
        payload = self.build_save_payload(draft)
        preflight = self.live_preflight(draft)
        result = {"ok": bool(preflight.get("ok")), "dry_run": True, "payload": payload, "preflight": preflight}
        if preflight.get("ok"):
            result.update(self.issue_publish_token(draft))
        return result

    async def publish_draft(
        self,
        draft: dict[str, Any],
        *,
        dry_run: bool = True,
        confirm_token: str = "",
        confirmation: str = "",
    ) -> dict[str, Any]:
        payload = self.build_save_payload(draft)
        if dry_run:
            return {"ok": True, "dry_run": True, "payload": payload}

        preflight = self.live_preflight(draft)
        if not preflight.get("ok"):
            return {
                "ok": False,
                "dry_run": False,
                "payload": payload,
                "preflight": preflight,
                "code": preflight["code"],
                "detail": preflight["detail"],
            }
        confirmed = self.validate_publish_confirmation(draft, token=confirm_token, confirmation=confirmation)
        if not confirmed.get("ok"):
            return {
                "ok": False,
                "dry_run": False,
                "payload": payload,
                "preflight": preflight,
                "confirmation": confirmed,
                "code": confirmed["code"],
                "detail": confirmed["detail"],
            }

        cookies = await self._web_cookies()
        if not cookies:
            return {
                "ok": False,
                "dry_run": False,
                "payload": payload,
                "code": "no_cookies",
                "detail": "Session Hub/env cookies are required for live Kwork publication.",
            }

        client = KworkWebListingClient(cookies)
        web_state = await client.open_new()
        if not web_state.get("ok"):
            return {
                "ok": False,
                "dry_run": False,
                "payload": payload,
                "web_state": web_state,
                "code": web_state.get("code") or "new_form_unavailable",
                "detail": web_state.get("detail") or "Kwork /new form is not available.",
            }

        live_draft = {**draft}
        hidden = web_state.get("hidden_fields") if isinstance(web_state.get("hidden_fields"), dict) else {}
        if hidden:
            live_draft["hidden_fields"] = {**hidden, **(live_draft.get("hidden_fields") or {})}
        live_draft.update(
            {key: value for key, value in hidden.items() if key not in live_draft and value not in (None, "")}
        )
        if web_state.get("csrftoken") and not live_draft.get("csrftoken"):
            live_draft["csrftoken"] = web_state["csrftoken"]
        if web_state.get("draft_id") and not live_draft.get("draft_id"):
            live_draft["draft_id"] = web_state["draft_id"]

        cover_path = live_draft.get("cover_image_path") or live_draft.get("image_path")
        if cover_path and not live_draft.get("cover_upload"):
            upload = await client.upload_cover(
                str(cover_path),
                category_id=int(live_draft.get("category_id") or 0),
                draft_id=live_draft.get("draft_id"),
                lang=str(live_draft.get("lang") or "ru"),
            )
            if not upload.get("ok"):
                payload = self.build_save_payload(live_draft)
                return {
                    "ok": False,
                    "dry_run": False,
                    "payload": payload,
                    "web_state": web_state,
                    "upload": upload,
                    "code": upload.get("code") or "cover_upload_failed",
                    "detail": upload.get("detail") or "Kwork cover upload failed.",
                }
            live_draft["cover_upload"] = upload
            live_draft["cover_upload"]["crop"] = upload.get("crop") or _default_cover_crop(str(cover_path))
            if upload.get("draft_id") and not live_draft.get("draft_id"):
                live_draft["draft_id"] = upload["draft_id"]

        portfolio_uploads: list[dict[str, Any]] = []
        required_portfolio_count = _required_portfolio_count(live_draft)
        portfolios = live_draft.get("portfolios") if isinstance(live_draft.get("portfolios"), list) else []
        if required_portfolio_count and len(portfolios) < required_portfolio_count:
            assets = self.ensure_portfolio_assets(live_draft)
            if len(assets) < required_portfolio_count:
                payload = self.build_save_payload(live_draft)
                return {
                    "ok": False,
                    "dry_run": False,
                    "payload": payload,
                    "web_state": web_state,
                    "code": "portfolio_generation_failed",
                    "detail": "Kwork requires at least five portfolio works for this category.",
                }

            known_hashes: list[str] = []
            cover_upload = live_draft.get("cover_upload") if isinstance(live_draft.get("cover_upload"), dict) else {}
            if cover_upload.get("first_photo_hash"):
                known_hashes.append(str(cover_upload["first_photo_hash"]))
            generated_portfolios: list[dict[str, Any]] = []
            for index, asset in enumerate(assets[:required_portfolio_count]):
                upload = await client.upload_portfolio_image(
                    str(asset["path"]),
                    known_hashes=known_hashes,
                    kwork_id=live_draft.get("kwork_id"),
                )
                portfolio_uploads.append({"asset": asset, "upload": upload})
                if not upload.get("ok"):
                    payload = self.build_save_payload(live_draft)
                    return {
                        "ok": False,
                        "dry_run": False,
                        "payload": payload,
                        "web_state": web_state,
                        "portfolio_uploads": portfolio_uploads,
                        "code": upload.get("code") or "portfolio_upload_failed",
                        "detail": upload.get("detail") or "Kwork portfolio image upload failed.",
                    }
                if upload.get("hash"):
                    known_hashes.append(str(upload["hash"]))
                generated_portfolios.append(_portfolio_payload(upload, str(asset.get("title") or "Работа"), index))
            live_draft["portfolio_assets"] = assets[:required_portfolio_count]
            live_draft["portfolios"] = generated_portfolios

        payload = self.build_save_payload(live_draft)
        save_method = getattr(client, "save_kwork_json", client.save_kwork)
        save_result = await save_method(payload["form_payload"], referer=web_state.get("final_url"))
        verify_result = await client.verify_saved_kwork(save_result, live_draft) if save_result.get("ok") else None
        verified = bool(verify_result and verify_result.get("ok"))
        verify_code = str(verify_result.get("code") or "") if isinstance(verify_result, dict) else ""
        save_code = str(save_result.get("code") or "")
        manual_verification = (
            verify_code == "manual_verification_required" or save_code == "manual_verification_required"
        )
        return {
            "ok": bool(save_result.get("ok") and verified),
            "dry_run": False,
            "payload": payload,
            "web_state": web_state,
            "portfolio_uploads": portfolio_uploads,
            "save_result": save_result,
            "verify_result": verify_result,
            "code": (
                "verified"
                if verified
                else "manual_verification_required"
                if manual_verification
                else "post_save_verification_failed"
                if verify_result
                else save_result.get("code") or ("success" if save_result.get("ok") else "save_failed")
            ),
            "detail": (
                ""
                if verified
                else verify_result.get("detail")
                if isinstance(verify_result, dict) and manual_verification and verify_result.get("detail")
                else save_result.get("detail")
                if manual_verification and save_result.get("detail")
                else save_result.get("errors")
                or save_result.get("detail")
                or (verify_result.get("code") if verify_result else "")
            ),
        }

    async def check_web_publish_session(self) -> dict[str, Any]:
        cookies = await self._web_cookies()
        if not cookies:
            return {"ok": False, "code": "no_cookies", "detail": "Session Hub не вернул cookies Kwork."}

        try:
            return await KworkWebListingClient(cookies).open_new()
        except Exception as exc:
            return {"ok": False, "code": "session_check_error", "detail": f"{type(exc).__name__}: {exc}"}

    async def _web_cookies(self) -> dict[str, str]:
        from src.platforms.kwork import get_kwork_service

        service = get_kwork_service()
        cookies = await service._fetch_session_hub_cookies()
        if not cookies:
            cookies = self._env_web_cookies()
        if cookies:
            probe = await KworkWebListingClient(cookies).open_new()
            if probe.get("ok"):
                return cookies
        refreshed = await service.refresh_web_session_cookies(url_to_redirect="/new")
        return refreshed or cookies

    @staticmethod
    def _env_web_cookies() -> dict[str, str]:
        from src.platforms.kwork import env_kwork_web_cookies

        return env_kwork_web_cookies()
