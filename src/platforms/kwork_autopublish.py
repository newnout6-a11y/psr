"""Kwork draft generation and guarded publication helpers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import time
from io import BytesIO
from pathlib import Path
from typing import Any
import zlib

import httpx
from loguru import logger

from src.paths import PROPOSAL_ASSETS_DIR, ensure_parent
from src.platforms.kwork_form_contract import attribute_manifest_hash, normalize_attribute_selection
from src.platforms.kwork_listing import KworkWebListingClient, selected_attribute_ids

API_BASE_URL = os.getenv("PSR_API_PUBLIC_BASE", "http://127.0.0.1:7788").rstrip("/")
_PUBLISH_CONFIRM_SECRET = secrets.token_hex(32)
PUBLISH_CONFIRMATION_PHRASE = "ОПУБЛИКОВАТЬ"


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


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)


def _write_rgb_png(path: Path, width: int, height: int, pixels: bytearray) -> None:
    rows = bytearray()
    stride = width * 3
    for y in range(height):
        rows.append(0)
        start = y * stride
        rows.extend(pixels[start : start + stride])

    payload = b"\x89PNG\r\n\x1a\n"
    payload += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    payload += _png_chunk(b"IDAT", zlib.compress(bytes(rows), level=6))
    payload += _png_chunk(b"IEND", b"")
    ensure_parent(path).write_bytes(payload)


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


def _image_api_url(endpoint: str, fallback_url: str) -> str:
    conn = _image_connection()
    base = (
        os.getenv("KWORK_COVER_IMAGE_BASE_URL")
        or str(conn.get("url") or "")
        or os.getenv("OPENAI_IMAGE_BASE_URL")
        or ""
    ).strip().rstrip("/")
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
        image_url = str(item.get("image_url") or item.get("cover") or "").strip()
        if not image_url or image_url in seen:
            continue
        seen.add(image_url)
        result.append(
            {
                "title": str(item.get("title") or "")[:160],
                "image_url": image_url,
                "price": str(item.get("price") or ""),
                "service_size": str(item.get("service_size") or ""),
            }
        )
        if len(result) >= max_items:
            break
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


def _blend(a: int, b: int, t: float) -> int:
    return max(0, min(255, int(a + (b - a) * t)))


def _generate_local_cover_png(path: Path, title: str, context: str = "") -> None:
    width, height = 1200, 800
    seed = hashlib.sha256(f"{title}|{context}".encode("utf-8", errors="ignore")).digest()
    accent = (80 + seed[0] % 130, 90 + seed[1] % 120, 120 + seed[2] % 100)
    warm = (190 + seed[3] % 50, 95 + seed[4] % 80, 40 + seed[5] % 80)
    cool = (25 + seed[6] % 50, 80 + seed[7] % 95, 115 + seed[8] % 95)
    pixels = bytearray(width * height * 3)

    def set_pixel(x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < width and 0 <= y < height:
            index = (y * width + x) * 3
            pixels[index : index + 3] = bytes(color)

    def fill_rect(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int], alpha: float = 1.0) -> None:
        x0, x1 = max(0, x0), min(width, x1)
        y0, y1 = max(0, y0), min(height, y1)
        for y in range(y0, y1):
            for x in range(x0, x1):
                index = (y * width + x) * 3
                if alpha >= 1:
                    pixels[index : index + 3] = bytes(color)
                else:
                    pixels[index] = _blend(pixels[index], color[0], alpha)
                    pixels[index + 1] = _blend(pixels[index + 1], color[1], alpha)
                    pixels[index + 2] = _blend(pixels[index + 2], color[2], alpha)

    def stroke_rect(x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int], thickness: int = 3) -> None:
        fill_rect(x0, y0, x1, y0 + thickness, color)
        fill_rect(x0, y1 - thickness, x1, y1, color)
        fill_rect(x0, y0, x0 + thickness, y1, color)
        fill_rect(x1 - thickness, y0, x1, y1, color)

    def fill_circle(cx: int, cy: int, radius: int, color: tuple[int, int, int], alpha: float = 1.0) -> None:
        r2 = radius * radius
        for y in range(cy - radius, cy + radius + 1):
            for x in range(cx - radius, cx + radius + 1):
                if (x - cx) * (x - cx) + (y - cy) * (y - cy) <= r2:
                    index = (y * width + x) * 3
                    if 0 <= x < width and 0 <= y < height:
                        pixels[index] = _blend(pixels[index], color[0], alpha)
                        pixels[index + 1] = _blend(pixels[index + 1], color[1], alpha)
                        pixels[index + 2] = _blend(pixels[index + 2], color[2], alpha)

    for y in range(height):
        vertical = y / max(1, height - 1)
        for x in range(width):
            horizontal = x / max(1, width - 1)
            noise = seed[(x * 17 + y * 31) % len(seed)] % 19
            r = _blend(15, cool[0], horizontal * 0.45) + noise // 5
            g = _blend(20, accent[1], vertical * 0.35) + noise // 7
            b = _blend(28, cool[2], (horizontal + vertical) * 0.25) + noise // 6
            index = (y * width + x) * 3
            pixels[index : index + 3] = bytes((min(r, 255), min(g, 255), min(b, 255)))

    fill_circle(950, 170, 210, accent, 0.28)
    fill_circle(160, 650, 240, warm, 0.18)
    fill_rect(140, 150, 760, 610, (18, 24, 32), 0.88)
    stroke_rect(140, 150, 760, 610, (75, 92, 110), 4)
    fill_rect(190, 210, 700, 260, accent, 0.85)
    for i in range(5):
        y = 305 + i * 48
        fill_rect(190, y, 470 + i * 35, y + 18, (205, 218, 230), 0.75)
        fill_rect(190, y + 25, 650 - i * 22, y + 35, (105, 127, 145), 0.65)

    phone_x, phone_y = 805, 225
    fill_rect(phone_x, phone_y, phone_x + 230, phone_y + 380, (12, 17, 24), 0.95)
    stroke_rect(phone_x, phone_y, phone_x + 230, phone_y + 380, warm, 5)
    fill_rect(phone_x + 25, phone_y + 55, phone_x + 205, phone_y + 100, accent, 0.75)
    for i in range(4):
        y = phone_y + 135 + i * 55
        fill_rect(phone_x + 28, y, phone_x + 172, y + 28, (225, 232, 238), 0.78)
        fill_circle(phone_x + 190, y + 14, 13, warm, 0.9)

    for i in range(7):
        x = 115 + i * 155
        y = 690 + ((seed[i] % 3) - 1) * 20
        fill_circle(x, y, 26, warm if i % 2 else accent, 0.8)
        if i:
            fill_rect(x - 129, y - 4, x - 28, y + 4, (210, 220, 230), 0.45)

    _write_rgb_png(path, width, height, pixels)


def _cover_text_from_draft(draft: dict[str, Any], request: dict[str, Any]) -> tuple[str, str]:
    explicit = str(request.get("cover_text") or "").strip()
    title = explicit or str(draft.get("title") or request.get("service_summary") or "Kwork").strip()
    title = re.sub(r"^(Сделаю|Разработаю|Настрою)\s+", "", title, flags=re.I).strip()
    title = title.replace("Telegram-бота или скрипт автоматизации", "Telegram-бот или скрипт")
    title = title.replace("телеграм бота или автоматизацию", "Telegram-бот или автоматизация")
    subtitle = str(request.get("cover_subtitle") or request.get("audience") or draft.get("auditory") or "").strip()
    if subtitle:
        subtitle = f"для {subtitle}" if not subtitle.lower().startswith("для ") else subtitle
    return _truncate(title, 54), _truncate(subtitle, 42)


def _wrap_words(text: str, max_chars: int, max_lines: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    return lines[:max_lines] or [text[:max_chars]]


def _overlay_cover_offer(path: Path, draft: dict[str, Any], request: dict[str, Any]) -> bool:
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont
    except Exception as exc:
        logger.debug(f"KworkAutopublish: Pillow unavailable for cover text overlay: {exc}")
        return False

    try:
        image = Image.open(path).convert("RGBA")
        width, height = image.size
        title, subtitle = _cover_text_from_draft(draft, request)
        title_lines = _wrap_words(title, 18 if len(title) > 28 else 22, 3)
        subtitle_lines = _wrap_words(subtitle, 28, 2) if subtitle else []

        configured_font = os.getenv("KWORK_COVER_FONT", "").strip()
        font_candidates: list[Path] = []
        if configured_font:
            font_candidates.append(Path(configured_font))
        font_candidates.extend(
            [
                Path("C:/Windows/Fonts/arialbd.ttf"),
                Path("C:/Windows/Fonts/segoeuib.ttf"),
                Path("C:/Windows/Fonts/arial.ttf"),
            ]
        )
        font_path = next((item for item in font_candidates if item.is_file()), None)
        title_size = max(44, min(82, width // 16))
        subtitle_size = max(24, min(38, width // 32))
        if font_path:
            title_font = ImageFont.truetype(str(font_path), title_size)
            subtitle_font = ImageFont.truetype(str(font_path), subtitle_size)
        else:
            title_font = ImageFont.load_default()
            subtitle_font = ImageFont.load_default()

        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        margin = int(width * 0.055)
        box_w = int(width * 0.55)
        line_gap = int(title_size * 0.16)
        subtitle_gap = int(subtitle_size * 0.55) if subtitle_lines else 0
        title_heights = [
            draw.textbbox((0, 0), line, font=title_font, stroke_width=1)[3] for line in title_lines
        ]
        subtitle_heights = [draw.textbbox((0, 0), line, font=subtitle_font)[3] for line in subtitle_lines]
        text_h = sum(title_heights) + line_gap * max(0, len(title_lines) - 1)
        text_h += subtitle_gap + sum(subtitle_heights) + int(subtitle_size * 0.12) * max(0, len(subtitle_lines) - 1)
        box_h = min(int(height * 0.48), text_h + int(height * 0.12))
        x0 = margin
        y0 = int(height * 0.12)
        x1 = x0 + box_w
        y1 = y0 + box_h

        shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow)
        shadow_draw.rounded_rectangle((x0 + 10, y0 + 12, x1 + 10, y1 + 12), radius=28, fill=(0, 0, 0, 145))
        shadow = shadow.filter(ImageFilter.GaussianBlur(10))
        overlay.alpha_composite(shadow)
        draw.rounded_rectangle((x0, y0, x1, y1), radius=28, fill=(5, 8, 14, 196), outline=(255, 255, 255, 58), width=2)
        draw.rectangle((x0, y0, x0 + 12, y1), fill=(255, 122, 24, 235))

        cursor_y = y0 + int(height * 0.055)
        text_x = x0 + int(width * 0.045)
        for line in title_lines:
            draw.text(
                (text_x, cursor_y),
                line,
                font=title_font,
                fill=(255, 255, 255, 255),
                stroke_width=2,
                stroke_fill=(0, 0, 0, 150),
            )
            bbox = draw.textbbox((text_x, cursor_y), line, font=title_font, stroke_width=2)
            cursor_y = bbox[3] + line_gap

        if subtitle_lines:
            cursor_y += subtitle_gap
            for line in subtitle_lines:
                draw.text((text_x, cursor_y), line, font=subtitle_font, fill=(255, 190, 115, 255))
                bbox = draw.textbbox((text_x, cursor_y), line, font=subtitle_font)
                cursor_y = bbox[3] + int(subtitle_size * 0.12)

        image.alpha_composite(overlay)
        ensure_parent(path)
        image.convert("RGB").save(path, "PNG", optimize=True)
        return True
    except Exception as exc:
        logger.warning(f"KworkAutopublish: cover text overlay failed: {exc}")
        return False


def _should_overlay_cover_text(request: dict[str, Any]) -> bool:
    value = request.get("cover_text_overlay")
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return value is True


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

    async def generate_draft(self, request: dict[str, Any]) -> dict[str, Any]:
        category_id = int(request.get("category_id") or 0)
        if category_id <= 0:
            raise ValueError("category_id is required")

        draft = await self._generate_text(request)
        draft.update(
            {
                "category_id": category_id,
                "classifier_id": request.get("classifier_id"),
                "price": int(request.get("price") or draft.get("price") or 500),
                "work_time": int(request.get("work_time") or draft.get("work_time") or 3),
                "attributes": request.get("attributes") or {},
                "attribute_manifest": request.get("attribute_manifest") or {},
                "attribute_selection": request.get("attribute_selection") or request.get("attributes") or {},
                "lang": request.get("lang") or "ru",
            }
        )

        image = None
        if request.get("generate_image"):
            image = await self.generate_cover(draft, request)
            if isinstance(image, dict) and image.get("path"):
                draft["cover_image_path"] = image.get("path")
                draft["cover_image"] = image

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

    def _recent_cover_history(self, limit: int | None = None) -> list[dict[str, Any]]:
        max_items = _cover_history_limit() if limit is None else max(0, limit)
        if max_items <= 0:
            return []
        root = PROPOSAL_ASSETS_DIR / "kwork_autopublish"
        if not root.exists():
            return []

        result: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for sidecar in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            image_path = Path(str(data.get("path") or sidecar.with_suffix(".png")))
            if not image_path.is_file():
                image_path = sidecar.with_suffix(".png")
            data_url = _image_file_to_data_url(image_path) if image_path.is_file() else ""
            if image_path.is_file():
                seen_paths.add(str(image_path.resolve()))
            result.append(
                {
                    "created_at": data.get("created_at"),
                    "status": data.get("status"),
                    "title": data.get("title"),
                    "category": data.get("category"),
                    "classifier": data.get("classifier"),
                    "prompt": _truncate(str(data.get("prompt") or ""), 700),
                    "prompt_source": data.get("prompt_source"),
                    "visual_style_brief": _truncate(str(data.get("visual_style_brief") or ""), 500),
                    "path": str(image_path) if image_path.is_file() else "",
                    "data_url": data_url,
                }
            )
            if len(result) >= max_items:
                break
        if len(result) < max_items:
            for image_path in sorted(root.glob("*.png"), key=lambda item: item.stat().st_mtime, reverse=True):
                resolved = str(image_path.resolve())
                if resolved in seen_paths:
                    continue
                result.append(
                    {
                        "created_at": "",
                        "status": "orphan_png",
                        "title": image_path.stem,
                        "category": "",
                        "classifier": "",
                        "prompt": "",
                        "prompt_source": "unknown",
                        "visual_style_brief": "",
                        "path": str(image_path),
                        "data_url": _image_file_to_data_url(image_path),
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
        analysis = request.get("_cover_visual_analysis") if isinstance(request.get("_cover_visual_analysis"), dict) else {}
        payload = {
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
            "visual_analysis_status": analysis.get("status"),
            "visual_style_brief": analysis.get("brief"),
            "competitor_image_urls": analysis.get("image_urls") or [],
            "competitor_images_seen": analysis.get("images_seen") or 0,
            "cover_prompt_context": request.get("_cover_prompt_context") or {},
        }
        try:
            ensure_parent(_cover_sidecar_path(path)).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.debug(f"KworkAutopublish: cover sidecar write failed: {exc}")

    async def build_cover_prompt(self, draft: dict[str, Any], request: dict[str, Any]) -> tuple[str, str]:
        visual_analysis = await self.analyze_competitor_covers(draft, request)
        request["_cover_visual_analysis"] = visual_analysis
        cover_history = self._recent_cover_history()
        request["_cover_history"] = [{key: value for key, value in item.items() if key != "data_url"} for item in cover_history]
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
            model = request.get("cover_prompt_model") or os.getenv("KWORK_COVER_PROMPT_MODEL") or request.get("model") or None
            prompt_payload = json.dumps(self._cover_prompt_brief(draft, request, visual_analysis=visual_analysis), ensure_ascii=False)
            competitor_prompt_images = [str(item) for item in request.get("_cover_competitor_image_data_urls", []) if item]
            history_prompt_images = [str(item.get("data_url")) for item in cover_history if item.get("data_url")]
            prompt_images = [*competitor_prompt_images, *history_prompt_images][:8]
            competitor_images_sent = min(len(competitor_prompt_images), len(prompt_images))
            history_images_sent = max(0, len(prompt_images) - competitor_images_sent)
            request["_cover_prompt_context"] = _cover_prompt_context(
                visual_analysis,
                cover_history,
                prompt_images=prompt_images,
                competitor_images_sent=competitor_images_sent,
                history_images_sent=history_images_sent,
                route="llm_vision" if prompt_images else "llm_text_no_images",
            )
            system_prompt = (
                "You are an art director for Kwork cover images. "
                "Write one image-generation prompt in English. "
                "Return plain text only, no markdown, no JSON. "
                "First compare attached competitor covers and recent generated covers when images are provided. "
                "Choose a composition strategy that is visibly different from recent generated covers while still fitting the market. "
                "Do not default to the same dark SaaS dashboard or left text panel unless the provided visual evidence makes it clearly best. "
                "Avoid fake detailed UI screenshots, tiny unreadable interface text, random icons, and cluttered collage layouts. "
                "Prefer one clean commercial composition with a clear subject, strong hierarchy, premium lighting, and enough empty space for Russian text. "
                "Name concrete composition, palette, subject, text placement, and what should be better than the competitor average. "
                "Never copy competitor covers exactly, never include logos, contacts, watermarks, or brand names. "
                "The image model itself must draw the provided short Russian offer as clean readable text."
            )
            try:
                if prompt_images:
                    response = await router.generate_with_images(
                        prompt=prompt_payload,
                        image_urls=prompt_images,
                        provider=provider,
                        model=model,
                        temperature=float(request.get("cover_prompt_temperature") or 0.35),
                        max_tokens=900,
                        task="kwork_cover_prompt",
                        system_prompt=system_prompt,
                    )
                else:
                    response = await router.generate(
                        prompt=prompt_payload,
                        provider=provider,
                        model=model,
                        temperature=float(request.get("cover_prompt_temperature") or 0.35),
                        max_tokens=900,
                        task="kwork_cover_prompt",
                        system_prompt=system_prompt,
                    )
            except Exception as exc:
                if not prompt_images:
                    raise
                logger.warning(f"KworkAutopublish: cover prompt vision route failed, retrying text-only: {exc}")
                request["_cover_prompt_context"] = _cover_prompt_context(
                    visual_analysis,
                    cover_history,
                    prompt_images=prompt_images,
                    competitor_images_sent=competitor_images_sent,
                    history_images_sent=history_images_sent,
                    route="llm_text_after_vision_failure",
                    warning=f"{type(exc).__name__}: {exc}",
                )
                response = await router.generate(
                    prompt=prompt_payload,
                    provider=provider,
                    model=model,
                    temperature=float(request.get("cover_prompt_temperature") or 0.35),
                    max_tokens=900,
                    task="kwork_cover_prompt",
                    system_prompt=system_prompt,
                )
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
                    "Summarize the average visual style: composition, colors, objects, text placement, density, contrast, mood.",
                    "Name what the new cover should do better than the average competitor.",
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
                max_tokens=int(request.get("cover_vision_max_tokens") or os.getenv("KWORK_COVER_VISION_MAX_TOKENS", "700")),
                task="kwork_cover_vision",
                system_prompt=(
                    "You are a visual art director. Analyze marketplace cover images from pixels. "
                    "Be practical and concise. Never tell the image model to copy a competitor."
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
        downloaded: list[dict[str, str]] = []
        headers = {
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "User-Agent": "Mozilla/5.0 PSR-KworkCoverVision/1.0",
        }
        async with httpx.AsyncClient(timeout=_vision_timeout_seconds(), follow_redirects=True, trust_env=False) as client:
            for item in items:
                try:
                    response = await client.get(item["image_url"], headers=headers)
                    response.raise_for_status()
                    raw = response.content[: 5 * 1024 * 1024]
                    if not raw:
                        continue
                    downloaded.append({**item, "data_url": _image_bytes_to_data_url(raw, response.headers.get("content-type", ""))})
                except Exception as exc:
                    logger.debug(f"KworkAutopublish: competitor cover download failed: {item.get('image_url')} ({exc})")
        return downloaded

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
                    "description": _truncate(str(item.get("description") or ""), 450),
                    "service_size": item.get("service_size"),
                    "price": item.get("price"),
                }
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
            "competitor_cover_examples": competitor_examples,
            "competitor_visual_analysis": visual_analysis or {"status": "not_run", "brief": ""},
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
                "recent_generated_covers": len([item for item in (request.get("_cover_history") or []) if item.get("path")]),
            },
            "required_cover_text": {
                "title": _cover_text_from_draft(draft, request)[0],
                "subtitle": _cover_text_from_draft(draft, request)[1],
            },
            "requirements": [
                "3:2 cover composition for a freelance marketplace listing",
                "include the required Russian offer text directly inside the generated image",
                "choose the text placement from the visual analysis; use any calm high-contrast area that fits this specific cover",
                "make the Russian text short, legible, and typographically clean",
                "inspect competitor covers and recent generated covers when attached; explicitly avoid repeating our recent layouts",
                "the prompt must explain a specific visual strategy based on those attached images, not a reusable fixed template",
                "use competitor visual analysis as market context, then create a stronger and fresher cover for this exact service",
                "show the outcome visually, not a generic poster",
                "professional, credible, visually specific; not childish and not a generic SaaS poster",
                "no extra readable text beyond the required offer, no logos, no brand names, no contacts, no watermarks",
                "use competitor covers only as market context; do not copy them",
            ],
        }

    def _fallback_cover_prompt(
        self,
        draft: dict[str, Any],
        request: dict[str, Any],
        visual_analysis: dict[str, Any] | None = None,
    ) -> str:
        market_context = request.get("market_context") if isinstance(request.get("market_context"), dict) else {}
        competitors = market_context.get("competitors") or []
        visual_refs: list[str] = []
        if isinstance(competitors, list):
            for item in competitors[:4]:
                if isinstance(item, dict) and item.get("image_url"):
                    visual_refs.append(str(item["image_url"]))

        reference_line = f"Competitor visual references: {', '.join(visual_refs)}. " if visual_refs else ""
        analysis_brief = str((visual_analysis or {}).get("brief") or "").strip()
        analysis_line = f"Competitor visual analysis: {analysis_brief}. " if analysis_brief else ""
        history = request.get("_cover_history") if isinstance(request.get("_cover_history"), list) else []
        history_prompts = [
            _truncate(str(item.get("prompt") or item.get("visual_style_brief") or ""), 220)
            for item in history[:3]
            if isinstance(item, dict) and (item.get("prompt") or item.get("visual_style_brief"))
        ]
        history_line = (
            f"Recent generated cover directions to avoid repeating: {' | '.join(history_prompts)}. "
            if history_prompts
            else ""
        )
        cover_title, cover_subtitle = _cover_text_from_draft(draft, request)
        cover_text = f'Add large readable Russian text: "{cover_title}"'
        if cover_subtitle:
            cover_text += f' and smaller subtitle "{cover_subtitle}"'
        cover_text += ". "
        return (
            "Create a 3:2 Kwork cover image for a freelance service listing. "
            f"{cover_text}"
            "Place the text on a high-contrast calm area chosen for this composition; do not force a left-panel layout. "
            "No extra readable text, no logos, no brand names, no contacts, no watermarks. "
            "Avoid fake detailed UI screenshots, tiny unreadable interface text, random icon collages, and generic dark dashboard banners. "
            "Make it look like a premium, credible service preview with one clear subject, clean hierarchy, polished lighting, and a concrete outcome rather than abstract decoration. "
            f"Service title: {draft.get('title')}. "
            f"Audience: {request.get('audience') or draft.get('auditory') or ''}. "
            f"Category: {request.get('category_name') or ''}. "
            f"{analysis_line}"
            f"{history_line}"
            f"{reference_line}"
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
        return _truncate(text, 1800)

    def _fallback_draft(self, request: dict[str, Any]) -> dict[str, Any]:
        service = str(request.get("service_summary") or request.get("brief") or "автоматизацию под задачу")
        category = str(request.get("category_name") or "выбранной категории")
        title = _truncate(f"Сделаю {service}", 80)
        description = (
            f"Разработаю {service} для вашей задачи в категории {category}. "
            "Перед началом уточню цель, входные данные, ожидаемый результат и ограничения. "
            "После согласования подготовлю рабочее решение, проверю основные сценарии и передам понятные инструкции. "
            "Если у вас уже есть пример, сайт, таблица, бот или техническое задание, используем это как контекст для более точного результата."
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
        normalized = {
            "title": _truncate(str(parsed.get("title") or fallback["title"]), 80),
            "description": str(parsed.get("description") or fallback["description"]).strip(),
            "instruction": str(parsed.get("instruction") or fallback["instruction"]).strip(),
            "auditory": str(parsed.get("auditory") or fallback["auditory"]).strip(),
            "service_size": str(parsed.get("service_size") or fallback["service_size"]).strip(),
            "volume": str(parsed.get("volume") or fallback["volume"]).strip(),
            "price": int(parsed.get("price") or fallback["price"]),
            "work_time": int(parsed.get("work_time") or fallback["work_time"]),
            "faq": faq[:5],
        }
        return normalized

    async def generate_cover(self, draft: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        try:
            from src.action.proposal_image import ProposalImageGenerator, _first_openai_key, _openai_url
        except Exception as exc:
            return {"status": "error", "detail": f"image helpers unavailable: {exc}"}

        prompt, prompt_source = await self.build_cover_prompt(draft, request)
        title_slug = _slug(str(draft.get("title") or "kwork-cover"))
        image_suffix = hashlib.sha1(f"{prompt}|{time.time_ns()}".encode("utf-8", errors="ignore")).hexdigest()[:10]
        path = PROPOSAL_ASSETS_DIR / "kwork_autopublish" / f"{title_slug}-{image_suffix}.png"
        fallback_url = _openai_url("images/generations")
        api_key = _image_api_key(_first_openai_key())
        if not api_key:
            return self.generate_local_cover(path, draft, request, prompt, "OPENAI_API_KEY/OPENAI_API_KEYS is not configured")

        primary_payload = {
            "model": os.getenv("KWORK_COVER_IMAGE_MODEL", os.getenv("PROPOSAL_IMAGE_MODEL", "gpt-image-2")),
            "prompt": prompt,
            "size": os.getenv("KWORK_COVER_IMAGE_SIZE", "1536x1024"),
            "quality": os.getenv("KWORK_COVER_IMAGE_QUALITY", os.getenv("PROPOSAL_IMAGE_QUALITY", "medium")),
        }
        fallback_payloads = [
            primary_payload,
            {**primary_payload, "model": "gpt-image-1"},
            {**primary_payload, "model": "gpt-image-1", "size": "1024x1024", "quality": "standard"},
        ]
        payloads: list[dict[str, Any]] = []
        seen_payloads: set[str] = set()
        for candidate in fallback_payloads:
            key = json.dumps(candidate, sort_keys=True, ensure_ascii=False, default=str)
            if key not in seen_payloads:
                seen_payloads.add(key)
                payloads.append(candidate)

        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        errors: list[str] = []
        try:
            async with httpx.AsyncClient(timeout=_image_timeout_seconds(), trust_env=False) as client:
                data: dict[str, Any] = {}
                for index, payload in enumerate(payloads):
                    response = await client.post(_image_api_url("images/generations", fallback_url), headers=headers, json=payload)
                    if response.status_code in {400, 422} and index < len(payloads) - 1:
                        errors.append(
                            f"{payload.get('model')} {payload.get('size')} quality={payload.get('quality')}: "
                            f"{response.status_code} {response.text[:300]}"
                        )
                        continue
                    response.raise_for_status()
                    data = response.json()
                    if index:
                        request["_cover_image_payload_fallback"] = {
                            "model": payload.get("model"),
                            "size": payload.get("size"),
                            "quality": payload.get("quality"),
                            "previous_errors": errors,
                        }
                    break
            b64 = ProposalImageGenerator._extract_b64_image(data)
            if not b64:
                return self.generate_local_cover(path, draft, request, prompt, "image API returned no base64 image")
            path = ensure_parent(path)
            path.write_bytes(base64.b64decode(b64))
            normalized, normalize_detail = _normalize_cover_png(path)
            if not normalized:
                return self.generate_local_cover(
                    path,
                    draft,
                    request,
                    prompt,
                    f"generated cover validation failed: {normalize_detail}",
                    prompt_source,
                )
            overlay = _overlay_cover_offer(path, draft, request) if _should_overlay_cover_text(request) else False
            self._write_cover_sidecar(
                path,
                draft,
                request,
                prompt=prompt,
                prompt_source=prompt_source,
                status="generated",
            )
            return self._with_visual_analysis(
                self.image_result("generated", path, prompt=prompt, prompt_source=prompt_source, text_overlay=overlay),
                request,
            )
        except Exception as exc:
            logger.warning(f"KworkAutopublish: cover generation failed: {exc}")
            detail = f"{type(exc).__name__}: {exc}"
            if errors:
                detail = f"{detail}; image API retries: {' | '.join(errors)}"
            return self.generate_local_cover(path, draft, request, prompt, detail, prompt_source)

    def generate_local_cover(
        self,
        path: Path,
        draft: dict[str, Any],
        request: dict[str, Any],
        prompt: str,
        detail: str,
        prompt_source: str = "fallback",
    ) -> dict[str, Any]:
        try:
            context_for_seed = " | ".join(
                [
                    str(request.get("image_context") or request.get("service_summary") or ""),
                    str(prompt or ""),
                    json.dumps(request.get("_cover_prompt_context") or {}, ensure_ascii=False, sort_keys=True),
                ]
            )
            _generate_local_cover_png(
                ensure_parent(path),
                str(draft.get("title") or ""),
                context_for_seed,
            )
            overlay = _overlay_cover_offer(path, draft, request) if _should_overlay_cover_text(request) else False
            self._write_cover_sidecar(
                path,
                draft,
                request,
                prompt=prompt,
                prompt_source=prompt_source,
                status="generated_local_fallback",
                detail=detail,
            )
            return self._with_visual_analysis(
                self.image_result(
                    "generated_local_fallback",
                    path,
                    prompt=prompt,
                    detail=detail,
                    prompt_source=prompt_source,
                    text_overlay=overlay,
                ),
                request,
            )
        except Exception as exc:
            return {"status": "error", "detail": f"{detail}; local fallback failed: {type(exc).__name__}: {exc}"}

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
        return result

    @staticmethod
    def image_result(
        status: str,
        path: Path,
        *,
        prompt: str = "",
        detail: str = "",
        prompt_source: str = "",
        text_overlay: bool | None = None,
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
        if text_overlay is not None:
            result["text_overlay"] = text_overlay
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
        crop = cover_upload.get("first-kwork-photo-size[]") or cover_upload.get("crop") or draft.get("first-kwork-photo-size[]")
        if crop:
            pairs.append(
                (
                    "first-kwork-photo-size[]",
                    crop if isinstance(crop, str) else json.dumps(crop, ensure_ascii=False),
                )
            )
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
            "selection_hash": normalization["selection_hash"] if normalization is not None else draft.get("selection_hash"),
            "faq": faq,
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

    def validate_publish_confirmation(self, draft: dict[str, Any], token: str = "", confirmation: str = "") -> dict[str, Any]:
        phrase = os.getenv("KWORK_AUTOPUBLISH_CONFIRM_PHRASE", PUBLISH_CONFIRMATION_PHRASE)
        if confirmation.strip() != phrase:
            return {"ok": False, "code": "confirmation_phrase_required", "detail": f"Type {phrase} to publish live."}

        parts = str(token or "").split(".")
        if len(parts) != 3:
            return {"ok": False, "code": "publish_token_required", "detail": "Live publish requires a fresh preflight token."}

        issued_raw, token_hash, signature = parts
        try:
            issued_at = int(issued_raw)
        except ValueError:
            return {"ok": False, "code": "invalid_publish_token", "detail": "Publish token timestamp is invalid."}

        ttl = int(os.getenv("KWORK_AUTOPUBLISH_CONFIRM_TTL_SECONDS", "600") or "600")
        if issued_at < int(time.time()) - ttl:
            return {"ok": False, "code": "publish_token_expired", "detail": "Publish token expired; run preflight again."}

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
            value not in (None, "") and not (isinstance(value, list) and len(value) == 0) for value in selection.values()
        ):
            missing.append("attribute_selection")

        manifest = draft.get("attribute_manifest") if isinstance(draft.get("attribute_manifest"), dict) else {}
        if not manifest or ("controls" not in manifest and "unresolved_required" not in manifest):
            missing.append("attribute_manifest")
        unresolved = (
            normalization["unresolved_required"]
            if normalization is not None
            else manifest.get("unresolved_required") if isinstance(manifest.get("unresolved_required"), list) else []
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
        live_draft.update({key: value for key, value in hidden.items() if key not in live_draft and value not in (None, "")})
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

        payload = self.build_save_payload(live_draft)
        save_method = getattr(client, "save_kwork_json", client.save_kwork)
        save_result = await save_method(payload["form_payload"], referer=web_state.get("final_url"))
        verify_result = await client.verify_saved_kwork(save_result, live_draft) if save_result.get("ok") else None
        verified = bool(verify_result and verify_result.get("ok"))
        verify_code = str(verify_result.get("code") or "") if isinstance(verify_result, dict) else ""
        save_code = str(save_result.get("code") or "")
        manual_verification = verify_code == "manual_verification_required" or save_code == "manual_verification_required"
        return {
            "ok": bool(save_result.get("ok") and verified),
            "dry_run": False,
            "payload": payload,
            "web_state": web_state,
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
        return cookies or self._env_web_cookies()

    @staticmethod
    def _env_web_cookies() -> dict[str, str]:
        from src.platforms.kwork import env_kwork_web_cookies

        return env_kwork_web_cookies()
