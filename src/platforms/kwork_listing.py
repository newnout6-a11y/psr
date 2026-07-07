"""Kwork web listing form helpers.

Kwork's listing form is partly JSON and partly HTML fragments returned by
`/api/attribute/loadclassification`.  This module keeps the parsed form shape
close to the browser contract so autopublish does not lose dynamic radio and
checkbox fields.
"""

from __future__ import annotations

import re
import hashlib
import json
from pathlib import Path
from html import unescape
from typing import Any
from urllib.parse import urlencode

import httpx
from loguru import logger

KWORK_WEB_BASE_URL = "https://kwork.ru"

ATTRIBUTE_NAME_RE = re.compile(r"^(?:new_custom_)?attribute\[(\d+)\](\[\])?$")
FIELD_PART_RE = re.compile(r"([^\[\]]+)|\[([^\[\]]*)\]")
QUESTION_CLASS_RE = re.compile(r"(label|title|caption|name|field|parameter|param)", re.I)
MANUAL_VERIFICATION_RE = re.compile(
    r"(smartcaptcha|smart-captcha|smartcaptcha\.cloud\.yandex\.ru|captcha-api\.yandex|"
    r"smart-token|подтвердите,\s*что\s*вы\s*не\s*робот|я\s*не\s*робот|"
    r"большой\s+нагрузк|автоматическ(?:ие|ими)?\s+скрипт|manual_verification_required)",
    re.I,
)


def _clean_text(value: Any) -> str:
    text = unescape(str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def is_kwork_manual_verification_page(text: str, final_url: str = "") -> bool:
    haystack = f"{final_url}\n{text or ''}"
    return bool(MANUAL_VERIFICATION_RE.search(haystack))


def manual_verification_response(status_code: int, text: str, final_url: str = "") -> dict[str, Any] | None:
    if not is_kwork_manual_verification_page(text, final_url):
        return None
    logger.warning(f"Kwork manual_verification_required: status={status_code} url={str(final_url or '')[:180]}")
    return {
        "ok": False,
        "http_status": status_code,
        "code": "manual_verification_required",
        "detail": "Kwork requires a manual SmartCaptcha/robot check. Open the Kwork verification window in PSR and complete it by hand.",
        "final_url": final_url,
    }


def _as_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _attr_present(node: Any, name: str) -> bool:
    if not node or not node.has_attr(name):
        return False
    value = node.get(name)
    return value in ("", name) or str(value).lower() in {"1", "true", "yes", "required"}


def _control_id_from_name(name: str) -> tuple[int | None, bool]:
    match = ATTRIBUTE_NAME_RE.match(name or "")
    if not match:
        return None, False
    return int(match.group(1)), bool(match.group(2))


def _option_label(input_node: Any, soup: Any) -> str:
    label = ""
    node_id = input_node.get("id")
    if node_id and soup is not None:
        label_node = soup.find("label", attrs={"for": node_id})
        if label_node:
            label = label_node.get_text(" ", strip=True)
    if not label:
        parent = input_node.find_parent("label")
        if parent:
            label = parent.get_text(" ", strip=True)
    if not label:
        container = input_node.find_parent(["li", "div", "span"])
        if container:
            label = container.get_text(" ", strip=True)
    return _clean_text(label or input_node.get("value") or "")


def _node_contains(container: Any, node: Any) -> bool:
    if container is node:
        return True
    try:
        return any(descendant is node for descendant in container.descendants)
    except Exception:
        return False


def _clean_question_label(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    text = re.sub(r"\s*#\d+\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" :—-")
    lower = text.lower()
    if lower in {"да", "нет", "disabled", "checkbox", "radio", "select"}:
        return ""
    if ATTRIBUTE_NAME_RE.search(text):
        return ""
    if len(text) < 3 or len(text) > 140:
        return ""
    return text


def _is_option_label_for(candidate: Any, input_node: Any) -> bool:
    node_id = input_node.get("id")
    return bool(candidate and candidate.name == "label" and node_id and candidate.get("for") == node_id)


def _control_option_labels(input_node: Any) -> set[str]:
    labels: set[str] = set()
    name = str(input_node.get("name") or "")
    root = input_node.find_parent("form") or input_node.find_parent(["fieldset", "section", "div"]) or input_node.parent
    if not root:
        return labels
    if input_node.name == "select":
        for option in input_node.find_all("option"):
            label = _clean_text(option.get_text(" ", strip=True))
            if label:
                labels.add(label)
        return labels
    if not name:
        return labels
    for peer in root.find_all("input", attrs={"name": name}):
        label = _option_label(peer, root)
        if label:
            labels.add(label)
    return labels


def _control_question_label(input_node: Any) -> str:
    """Find the human question/field label that owns a Kwork dynamic control."""
    own_option_labels = _control_option_labels(input_node)
    for attr in ("aria-label", "data-label", "data-title", "data-name", "title"):
        label = _clean_question_label(input_node.get(attr))
        if label and label not in own_option_labels:
            return label

    for ancestor in input_node.parents:
        if not ancestor or ancestor.name in {"form", "body", "html"}:
            break

        for candidate in ancestor.find_all(True, class_=QUESTION_CLASS_RE):
            if _node_contains(candidate, input_node) or candidate.find(["input", "select", "textarea"]):
                continue
            if _is_option_label_for(candidate, input_node):
                continue
            label = _clean_question_label(candidate.get_text(" ", strip=True))
            if label and label not in own_option_labels:
                return label

        for child in ancestor.find_all(["th", "td", "label", "div", "span"], recursive=False):
            if _node_contains(child, input_node) or child.find(["input", "select", "textarea"]):
                continue
            if _is_option_label_for(child, input_node):
                continue
            label = _clean_question_label(child.get_text(" ", strip=True))
            if label and label not in own_option_labels:
                return label

    return ""


def parse_classification_html(html: str) -> list[dict[str, Any]]:
    """Extract Kwork dynamic attribute controls from a loadclassification HTML fragment."""
    if not html:
        return []

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")

    grouped: dict[str, dict[str, Any]] = {}
    custom_names: dict[int, list[str]] = {}

    for input_node in soup.find_all(["input", "select", "textarea"]):
        name = str(input_node.get("name") or "")
        group_id, multiple_from_name = _control_id_from_name(name)
        if not group_id:
            continue

        input_type = str(input_node.get("type") or input_node.name or "text").lower()
        if input_node.name == "select":
            input_type = "select"
        if input_node.name == "textarea":
            input_type = "textarea"
        if name.startswith("new_custom_attribute"):
            custom_names.setdefault(group_id, []).append(name)
            input_type = "custom_text"
        multiple = multiple_from_name or input_type == "checkbox" or bool(input_node.get("multiple"))
        question_label = _control_question_label(input_node)
        control = grouped.setdefault(
            name,
            {
                "group_id": group_id,
                "name": name,
                "custom_name": "",
                "label": question_label,
                "question": question_label,
                "type": "checkbox" if multiple else input_type,
                "multiple": multiple,
                "required": _attr_present(input_node, "required") or _attr_present(input_node, "data-required"),
                "options": [],
                "value": _clean_text(input_node.get("value") if input_node.name != "textarea" else input_node.get_text(" ", strip=True)),
                "placeholder": _clean_text(input_node.get("placeholder") or ""),
                "data": dict(input_node.attrs),
            },
        )
        control["required"] = bool(
            control["required"] or _attr_present(input_node, "required") or _attr_present(input_node, "data-required")
        )
        if question_label and not control.get("question"):
            control["label"] = question_label
            control["question"] = question_label

        if input_node.name == "select":
            for option in input_node.find_all("option"):
                value = _as_int(option.get("value"))
                if value is None:
                    continue
                control["options"].append(
                    {
                        "id": value,
                        "value": value,
                        "label": _clean_text(option.get_text(" ", strip=True)),
                        "selected": bool(option.get("selected")),
                        "disabled": bool(option.get("disabled")),
                        "has_child": bool(option.get("data-has-child") or option.get("data-child")),
                        "data": dict(option.attrs),
                    }
            )
            continue

        if input_type in {"text", "textarea", "custom_text"}:
            continue

        value = _as_int(input_node.get("value"))
        if value is None:
            continue
        attrs = dict(input_node.attrs)
        has_child = any(
            str(attrs.get(key, "")).lower() in {"1", "true", "yes"}
            for key in ("data-has-child", "data-child", "data-has_child")
        )
        control["options"].append(
            {
                "id": value,
                "value": value,
                "label": _option_label(input_node, soup),
                "selected": bool(input_node.get("checked")),
                "disabled": bool(input_node.get("disabled")),
                "has_child": has_child,
                "data": attrs,
            }
        )

    for control in grouped.values():
        names = custom_names.get(control["group_id"], [])
        control["custom_name"] = names[0] if names else ""
        seen: set[int] = set()
        unique_options = []
        for option in control["options"]:
            option_id = int(option["id"])
            if option_id in seen:
                continue
            seen.add(option_id)
            unique_options.append(option)
        control["options"] = unique_options

    return list(grouped.values())


def _selection_values(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, list):
        result = []
        for item in value:
            parsed = _as_int(item)
            if parsed is not None:
                result.append(parsed)
        return result
    parsed = _as_int(value)
    return [parsed] if parsed is not None else []


def selected_attribute_ids(selection: dict[str, Any]) -> list[int]:
    result: list[int] = []
    for value in selection.values():
        result.extend(_selection_values(value))
    return result


def _maybe_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def _field_parts(name: str) -> list[str]:
    parts: list[str] = []
    for match in FIELD_PART_RE.finditer(name):
        parts.append(match.group(1) if match.group(1) is not None else match.group(2) or "")
    return parts


def _ensure_list_item(items: list[Any], index: int, factory: Any) -> Any:
    while len(items) <= index:
        items.append(None)
    if items[index] is None:
        items[index] = factory()
    return items[index]


def _assign_bracket_value(target: dict[str, Any], root: str, tokens: list[str], value: Any) -> None:
    if not tokens:
        if root in target:
            previous = target[root]
            if isinstance(previous, list):
                previous.append(value)
            else:
                target[root] = [previous, value]
        else:
            target[root] = value
        return

    root_is_list = tokens[0] == "" or (root in {"faq"} and tokens[0].isdigit())
    target.setdefault(root, [] if root_is_list else {})
    current = target[root]
    for index, token in enumerate(tokens):
        is_last = index == len(tokens) - 1
        next_token = "" if is_last else tokens[index + 1]
        next_factory = list if next_token.isdigit() or next_token == "" else dict

        if isinstance(current, list):
            if token == "":
                if is_last:
                    current.append(value)
                    return
                current.append(next_factory())
                current = current[-1]
                continue
            item_index = int(token)
            if is_last:
                while len(current) <= item_index:
                    current.append(None)
                current[item_index] = value
                return
            current = _ensure_list_item(current, item_index, next_factory)
            continue

        if token == "":
            list_value = current.setdefault(root, [])
            if not isinstance(list_value, list):
                list_value = [list_value]
                current[root] = list_value
            if is_last:
                list_value.append(value)
                return
            list_value.append(next_factory())
            current = list_value[-1]
            continue

        if is_last:
            if token in current:
                previous = current[token]
                if isinstance(previous, list):
                    previous.append(value)
                else:
                    current[token] = [previous, value]
            else:
                current[token] = value
            return
        child = current.get(token)
        if child is None:
            child = next_factory()
            current[token] = child
        current = child


def form_pairs_to_structured_payload(form_payload: list[tuple[str, Any]]) -> dict[str, Any]:
    """Mirror Kwork's browser-side bracket-field conversion before save_kwork."""
    structured: dict[str, Any] = {}
    for name, raw_value in form_payload:
        value = _maybe_json(raw_value)
        if name == "first_photo_json":
            structured[name] = value
            continue
        if name == "first_photo_path":
            structured[name] = str(raw_value or "")
            continue
        if name == "first-kwork-photo":
            structured[name] = None if raw_value in ("", "null", None) else value
            continue
        if name == "first-kwork-photo-size[]":
            structured["first-kwork-photo-size"] = value
            continue

        parts = _field_parts(name)
        if not parts:
            structured[name] = value
            continue
        _assign_bracket_value(structured, parts[0], parts[1:], value)
    return structured


def _flatten_ints(value: Any) -> set[int]:
    result: set[int] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            parsed_key = _as_int(key)
            if parsed_key is not None:
                result.add(parsed_key)
            result.update(_flatten_ints(item))
        return result
    if isinstance(value, (list, tuple, set)):
        for item in value:
            result.update(_flatten_ints(item))
        return result
    parsed = _as_int(value)
    if parsed is not None:
        result.add(parsed)
    return result


def _apply_fragment_metadata(controls: list[dict[str, Any]], fragments: list[dict[str, Any]]) -> dict[str, Any]:
    disabled_ids: set[int] = set()
    selected_childs: dict[str, Any] = {}
    attribute_resolutions: dict[str, Any] = {}
    for fragment in fragments:
        disabled_ids.update(_flatten_ints(fragment.get("disableIds")))
        if isinstance(fragment.get("selectedChilds"), dict):
            selected_childs.update({str(key): value for key, value in fragment["selectedChilds"].items()})
        if isinstance(fragment.get("attributeResolutions"), dict):
            attribute_resolutions.update({str(key): value for key, value in fragment["attributeResolutions"].items()})

    for control in controls:
        options = control.get("options") if isinstance(control.get("options"), list) else []
        if options:
            for option in options:
                option_id = _as_int(option.get("id"))
                if option_id in disabled_ids:
                    option["disabled"] = True
            control["disabled"] = all(bool(option.get("disabled")) for option in options)
        else:
            control["disabled"] = bool(control.get("group_id") in disabled_ids)

    return {
        "disableIds": sorted(disabled_ids),
        "selectedChilds": selected_childs,
        "attributeResolutions": attribute_resolutions,
    }


def _is_control_satisfied(control: dict[str, Any], selected: dict[str, Any]) -> bool:
    if control.get("disabled"):
        return True
    value = selected.get(control["name"])
    if value in (None, ""):
        return False
    if isinstance(value, list):
        return any(item not in (None, "") for item in value)
    return True


def parse_new_form_snapshot(html: str, final_url: str = "") -> dict[str, Any]:
    """Parse `/new` page form metadata needed for save_kwork."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    form = (
        soup.select_one("form.js-kwork-save-form")
        or soup.find("form", attrs={"action": re.compile("save_kwork", re.I)})
        or soup.find("form", attrs={"id": re.compile("kwork", re.I)})
    )
    hidden: dict[str, str] = {}
    if form:
        for node in form.find_all(["input", "textarea", "select"]):
            name = str(node.get("name") or "")
            if not name:
                continue
            input_type = str(node.get("type") or "").lower()
            if input_type == "hidden" or name in {"csrftoken", "draft_id", "lang"}:
                hidden[name] = str(node.get("value") or "")

    csrf = hidden.get("csrftoken") or ""
    if not csrf:
        csrf_match = re.search(r'name=["\']csrftoken["\']\s+value=["\']([^"\']+)["\']', html or "", re.I)
        csrf = csrf_match.group(1) if csrf_match else ""

    draft_id = hidden.get("draft_id") or hidden.get("kwork_id") or ""
    if not draft_id:
        draft_match = re.search(r"window\.draftId\s*=\s*[\"']?(\d+)[\"']?", html or "")
        draft_id = draft_match.group(1) if draft_match else ""

    action = str((form.get("action") if form else "") or "/save_kwork")
    if action and action.startswith("/"):
        action = f"{KWORK_WEB_BASE_URL}{action}"

    return {
        "ok": bool(form or "save_kwork" in (html or "")),
        "final_url": final_url,
        "form_found": bool(form),
        "form_action": action or f"{KWORK_WEB_BASE_URL}/save_kwork",
        "form_method": str(form.get("method") if form else "post").lower() or "post",
        "hidden_fields": hidden,
        "csrftoken": csrf,
        "draft_id": draft_id,
        "has_save_marker": "save_kwork" in (html or ""),
    }


def parse_save_kwork_response(status_code: int, text: str, final_url: str = "") -> dict[str, Any]:
    """Normalize save_kwork JSON/HTML responses into a stable result shape."""
    raw_text = text or ""
    verification = manual_verification_response(status_code, raw_text, final_url)
    if verification:
        return verification

    data: Any = None
    try:
        data = json.loads(raw_text) if raw_text.strip() else {}
    except json.JSONDecodeError:
        data = None

    if isinstance(data, dict):
        result = data.get("result")
        code = data.get("code") or data.get("status")
        errors = data.get("errors") or data.get("error") or data.get("message")
        ok = result == "success" or data.get("success") is True
        return {
            "ok": bool(ok),
            "http_status": status_code,
            "code": "success" if ok else str(code or "validation_error"),
            "result": result,
            "redirect_url": data.get("redirectUrl") or data.get("redirect_url"),
            "errors": errors,
            "raw": data,
            "final_url": final_url,
        }

    login_required = "/login" in final_url or "login" in raw_text[:800].lower()
    return {
        "ok": False,
        "http_status": status_code,
        "code": "login_required" if login_required else "non_json_response",
        "detail": raw_text[:500],
        "final_url": final_url,
    }


def _saved_kwork_match(html: str, draft: dict[str, Any]) -> dict[str, Any]:
    title = _clean_text(draft.get("title") or "")
    draft_id = str(draft.get("draft_id") or "").strip()
    id_patterns = []
    if draft_id:
        escaped = re.escape(draft_id)
        id_patterns = [
            rf'data-kwork-id=["\']{escaped}["\']',
            rf"/edit\?id={escaped}\b",
            rf"/new\?draft_id={escaped}\b",
            rf"draft_id={escaped}\b",
        ]

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "lxml")
    candidates = soup.find_all(["article", "li", "tr", "div", "a"], limit=2000)
    page_text = _clean_text(soup.get_text(" ", strip=True))

    for node in candidates:
        fragment = str(node)
        text = _clean_text(node.get_text(" ", strip=True))
        id_found = bool(id_patterns and any(re.search(pattern, fragment) for pattern in id_patterns))
        title_found = bool(title and title in text)
        if id_found and title_found:
            return {"ok": True, "matched_by": ["draft_id", "title"], "kwork_id": draft_id}
        if id_found and not title:
            return {"ok": True, "matched_by": ["draft_id"], "kwork_id": draft_id}
        if title_found and not draft_id:
            return {"ok": True, "matched_by": ["title"], "kwork_id": ""}

    if draft_id and any(re.search(pattern, html or "") for pattern in id_patterns):
        if title and title not in page_text:
            return {"ok": False}
        if not title:
            return {"ok": True, "matched_by": ["draft_id"], "kwork_id": draft_id}
    if title and title in page_text and not draft_id:
        return {"ok": True, "matched_by": ["title"], "kwork_id": ""}
    return {"ok": False}


class KworkWebListingClient:
    """Read Kwork listing form fragments through the same endpoints the page uses."""

    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}

    async def open_new(self) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=KWORK_WEB_BASE_URL,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Referer": f"{KWORK_WEB_BASE_URL}/manage_kworks",
                "User-Agent": "Mozilla/5.0 PSR-KworkListing/1.0",
            },
            timeout=20.0,
            follow_redirects=True,
            trust_env=False,
        ) as client:
            response = await client.get("/new", cookies=self.cookies)
            final_url = str(response.url)
            verification = manual_verification_response(response.status_code, response.text, final_url)
            if verification:
                return verification
            if "/login" in final_url:
                return {
                    "ok": False,
                    "code": "login_required",
                    "final_url": final_url,
                    "detail": "Kwork redirected /new to login.",
                }
            response.raise_for_status()
        snapshot = parse_new_form_snapshot(response.text, final_url=final_url)
        if not snapshot.get("ok"):
            snapshot["code"] = "new_form_unavailable"
            snapshot["detail"] = "Kwork /new did not contain a recognizable save form."
        return snapshot

    async def load_classification(self, category_id: int, attribute_id: int | None = None, lang: str = "ru") -> dict[str, Any]:
        params: dict[str, Any] = {"categoryId": category_id, "lang": lang}
        if attribute_id:
            params["attributeId"] = attribute_id
        async with httpx.AsyncClient(
            base_url=KWORK_WEB_BASE_URL,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": f"{KWORK_WEB_BASE_URL}/new",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": "Mozilla/5.0 PSR-KworkListing/1.0",
            },
            timeout=20.0,
            follow_redirects=True,
            trust_env=False,
        ) as client:
            response = await client.get("/api/attribute/loadclassification", params=params, cookies=self.cookies)
            verification = manual_verification_response(response.status_code, response.text, str(response.url))
            if verification:
                return {
                    "success": False,
                    "category_id": category_id,
                    "attribute_id": attribute_id,
                    "lang": lang,
                    "html": "",
                    "controls": [],
                    "raw_keys": [],
                    **verification,
                }
            response.raise_for_status()
            try:
                data = response.json() if response.content else {}
            except json.JSONDecodeError:
                final_url = str(response.url)
                login_required = "/login" in final_url or "login" in (response.text or "")[:800].lower()
                return {
                    "success": False,
                    "ok": False,
                    "category_id": category_id,
                    "attribute_id": attribute_id,
                    "lang": lang,
                    "html": "",
                    "controls": [],
                    "raw_keys": [],
                    "http_status": response.status_code,
                    "code": "login_required" if login_required else "non_json_loadclassification_response",
                    "detail": (response.text or "")[:500],
                    "final_url": final_url,
                }
        html = str(data.get("html") or "")
        return {
            "success": bool(data.get("success", True)),
            "category_id": category_id,
            "attribute_id": attribute_id,
            "lang": lang,
            "html": html,
            "selectedCount": data.get("selectedCount"),
            "count": data.get("count"),
            "disableIds": data.get("disableIds") or {},
            "selectedChilds": data.get("selectedChilds") or {},
            "attributeResolutions": data.get("attributeResolutions") or {},
            "controls": parse_classification_html(html),
            "raw_keys": sorted(data.keys()) if isinstance(data, dict) else [],
        }

    async def upload_cover(
        self,
        path: str | Path,
        *,
        category_id: int,
        draft_id: str | int | None = None,
        lang: str = "ru",
    ) -> dict[str, Any]:
        file_path = Path(path)
        if not file_path.is_file():
            return {"ok": False, "code": "cover_file_missing", "detail": str(file_path)}

        file_bytes = file_path.read_bytes()
        digest = hashlib.md5(file_bytes).hexdigest()
        data: dict[str, Any] = {
            "category_id": str(category_id),
            "validator": "KworkCover",
            "kworkLang": lang,
            "hashes[0]": digest,
        }
        if draft_id:
            data["kwork_id"] = str(draft_id)

        async with httpx.AsyncClient(
            base_url=KWORK_WEB_BASE_URL,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": f"{KWORK_WEB_BASE_URL}/new",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": "Mozilla/5.0 PSR-KworkListing/1.0",
            },
            timeout=60.0,
            follow_redirects=True,
            trust_env=False,
        ) as client:
            with file_path.open("rb") as fh:
                response = await client.post(
                    "/temp-image-upload",
                    cookies=self.cookies,
                    data=data,
                    files={"file": (file_path.name, fh, "image/png")},
                )
            if "/login" in str(response.url):
                return {"ok": False, "code": "login_required", "final_url": str(response.url)}
            response.raise_for_status()
            try:
                payload = response.json()
            except json.JSONDecodeError:
                return {
                    "ok": False,
                    "code": "non_json_upload_response",
                    "http_status": response.status_code,
                    "detail": response.text[:500],
                }

        image_data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        image_path = image_data.get("src") or image_data.get("image_path") or image_data.get("path") or image_data.get("name")
        upload_draft_id = image_data.get("kwork_id") or image_data.get("draft_id") or payload.get("kwork_id") or payload.get("draft_id") or draft_id
        if not image_path:
            return {
                "ok": False,
                "code": "cover_upload_missing_path",
                "detail": "Kwork cover upload response did not include image_path/path/src.",
                "raw": payload,
                "first_photo_hash": image_data.get("image_hash") or image_data.get("hash") or "",
                "draft_id": str(upload_draft_id or "") if upload_draft_id else "",
            }
        return {
            "ok": True,
            "raw": payload,
            "first_photo_json": payload,
            "first_photo_path": image_path or "",
            "first_photo_hash": image_data.get("image_hash") or image_data.get("hash") or "",
            "draft_id": str(upload_draft_id or "") if upload_draft_id else "",
        }

    async def save_kwork(self, form_payload: list[tuple[str, str]], referer: str | None = None) -> dict[str, Any]:
        encoded = urlencode(form_payload).encode("utf-8")
        async with httpx.AsyncClient(
            base_url=KWORK_WEB_BASE_URL,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": KWORK_WEB_BASE_URL,
                "Referer": referer or f"{KWORK_WEB_BASE_URL}/new",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": "Mozilla/5.0 PSR-KworkListing/1.0",
            },
            timeout=60.0,
            follow_redirects=True,
            trust_env=False,
        ) as client:
            response = await client.post("/save_kwork", cookies=self.cookies, content=encoded)
        return parse_save_kwork_response(response.status_code, response.text, final_url=str(response.url))

    async def save_kwork_json(self, form_payload: list[tuple[str, Any]], referer: str | None = None) -> dict[str, Any]:
        structured = form_pairs_to_structured_payload(form_payload)
        async with httpx.AsyncClient(
            base_url=KWORK_WEB_BASE_URL,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Origin": KWORK_WEB_BASE_URL,
                "Referer": referer or f"{KWORK_WEB_BASE_URL}/new",
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": "Mozilla/5.0 PSR-KworkListing/1.0",
            },
            timeout=60.0,
            follow_redirects=True,
            trust_env=False,
        ) as client:
            response = await client.post("/save_kwork", cookies=self.cookies, json=structured)
        return parse_save_kwork_response(response.status_code, response.text, final_url=str(response.url))

    async def verify_saved_kwork(self, save_result: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
        """Best-effort proof that a saved Kwork is visible after save_kwork success."""
        if not save_result.get("ok"):
            return {"ok": False, "code": "save_not_successful"}

        targets: list[str] = []
        redirect_url = str(save_result.get("redirect_url") or "").strip()
        if redirect_url:
            targets.append(redirect_url)
        targets.extend(
            [
                "/manage_kworks?group=moderated",
                "/manage_kworks?group=active",
                "/manage_kworks?group=draft",
                "/manage_kworks?group=paused",
                "/manage_kworks?group=rejected",
                "/manage_kworks",
            ]
        )

        title = _clean_text(draft.get("title") or "")

        async with httpx.AsyncClient(
            base_url=KWORK_WEB_BASE_URL,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/json",
                "Referer": f"{KWORK_WEB_BASE_URL}/manage_kworks",
                "User-Agent": "Mozilla/5.0 PSR-KworkListing/1.0",
            },
            timeout=30.0,
            follow_redirects=True,
            trust_env=False,
        ) as client:
            checked: list[dict[str, Any]] = []
            for target in dict.fromkeys(targets):
                response = await client.get(target, cookies=self.cookies)
                final_url = str(response.url)
                text = response.text or ""
                verification = manual_verification_response(response.status_code, text, final_url)
                if verification:
                    return verification
                if "/login" in final_url:
                    return {"ok": False, "code": "login_required", "final_url": final_url}
                match = _saved_kwork_match(text, draft)
                checked.append(
                    {
                        "url": target,
                        "final_url": final_url,
                        "status": response.status_code,
                        "matched": bool(match.get("ok")),
                        "matched_by": match.get("matched_by", []),
                    }
                )
                if response.status_code < 400 and match.get("ok"):
                    group_match = re.search(r"group=([a-z_]+)", target)
                    return {
                        "ok": True,
                        "code": "verified",
                        "method": "manage_html",
                        "status_group": group_match.group(1) if group_match else "",
                        "matched_by": match.get("matched_by", []),
                        "kwork_id": match.get("kwork_id", ""),
                        "title": title,
                        "url": target,
                        "checked": checked,
                    }

        return {"ok": False, "code": "not_found_after_save", "checked": checked}

    async def build_attribute_manifest(
        self,
        category_id: int,
        *,
        selection: dict[str, Any] | None = None,
        classifier_id: int | None = None,
        lang: str = "ru",
    ) -> dict[str, Any]:
        selected = dict(selection or {})
        root = await self.load_classification(category_id, lang=lang)
        controls = list(root["controls"])

        if classifier_id and root["controls"] and not selected:
            first = root["controls"][0]
            option_ids = {int(option["id"]) for option in first.get("options", []) if option.get("id") is not None}
            if int(classifier_id) in option_ids:
                selected[first["name"]] = int(classifier_id)

        visited: set[int] = set()
        queue = selected_attribute_ids(selected)
        fragments = [root]
        while queue:
            attribute_id = queue.pop(0)
            if attribute_id in visited:
                continue
            visited.add(attribute_id)
            fragment = await self.load_classification(category_id, attribute_id=attribute_id, lang=lang)
            fragments.append(fragment)
            controls.extend(fragment["controls"])
            for next_id in selected_attribute_ids(selected):
                if next_id not in visited and next_id not in queue:
                    queue.append(next_id)

        deduped: dict[str, dict[str, Any]] = {}
        for control in controls:
            deduped[control["name"]] = control
        manifest_controls = list(deduped.values())
        metadata = _apply_fragment_metadata(manifest_controls, fragments)
        status_fragment = next((item for item in fragments if item.get("code")), None)

        return {
            "category_id": category_id,
            "lang": lang,
            "success": not bool(status_fragment),
            "code": status_fragment.get("code") if status_fragment else "",
            "detail": status_fragment.get("detail") if status_fragment else "",
            "final_url": status_fragment.get("final_url") if status_fragment else "",
            "http_status": status_fragment.get("http_status") if status_fragment else None,
            "selected": selected,
            "controls": manifest_controls,
            "metadata": metadata,
            "fragments": [
                {
                    "attribute_id": item["attribute_id"],
                    "count": item.get("count"),
                    "selectedCount": item.get("selectedCount"),
                    "disableIds": item.get("disableIds") or {},
                    "selectedChilds": item.get("selectedChilds") or {},
                    "attributeResolutions": item.get("attributeResolutions") or {},
                    "raw_keys": item.get("raw_keys", []),
                }
                for item in fragments
            ],
            "unresolved_required": [
                control["name"]
                for control in manifest_controls
                if control.get("required") and not _is_control_satisfied(control, selected)
            ],
        }
