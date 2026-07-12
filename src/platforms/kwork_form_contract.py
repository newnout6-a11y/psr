"""Canonical server-side contract for dynamic Kwork listing attributes.

The browser form is assembled from HTML fragments.  This module turns that
mutable representation into a stable snapshot and validates submitted option
IDs against the snapshot before a draft can be generated or published.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from typing import Any


MANIFEST_SCHEMA_VERSION = 1


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _as_id_values(value: Any) -> list[int]:
    raw_values = value if isinstance(value, (list, tuple, set, frozenset)) else [value]
    values: list[int] = []
    for item in raw_values:
        parsed = _as_int(item)
        if parsed is not None:
            values.append(parsed)
    return values


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _sha256(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()}"


def _parent_option_ids(control: Mapping[str, Any]) -> list[int]:
    values = control.get("parent_option_ids")
    if values is None:
        values = control.get("parent_attribute_ids")
    if values is None:
        values = control.get("source_attribute_ids")
    return sorted(set(_as_id_values(values)))


def _canonical_control(control: Mapping[str, Any]) -> dict[str, Any] | None:
    name = _as_text(control.get("name"))
    if not name:
        return None

    options: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    raw_options = control.get("options") if isinstance(control.get("options"), Sequence) else []
    for raw_option in raw_options:
        if not isinstance(raw_option, Mapping):
            continue
        option_id = _as_int(raw_option.get("id", raw_option.get("value")))
        if option_id is None or option_id in seen_ids:
            continue
        seen_ids.add(option_id)
        options.append(
            {
                "id": option_id,
                "label": _as_text(raw_option.get("label", raw_option.get("value"))),
                "disabled": bool(raw_option.get("disabled")),
                "has_child": bool(raw_option.get("has_child")),
            }
        )
    options.sort(key=lambda option: option["id"])

    group_id = _as_int(control.get("group_id"))
    return {
        "name": name,
        "group_id": group_id,
        "label": _as_text(control.get("label", control.get("question"))),
        "question": _as_text(control.get("question", control.get("label"))),
        "type": _as_text(control.get("type")) or "text",
        "multiple": bool(control.get("multiple")),
        "required": bool(control.get("required")),
        "disabled": bool(control.get("disabled")),
        "parent_option_ids": _parent_option_ids(control),
        "options": options,
    }


def canonicalize_attribute_manifest(manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the hashable subset of a dynamic Kwork form manifest.

    Labels remain presentation data.  The field transport name and option IDs
    are the contract used for submission.
    """

    source = manifest if isinstance(manifest, Mapping) else {}
    raw_controls = source.get("controls") if isinstance(source.get("controls"), Sequence) else []
    controls: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for raw_control in raw_controls:
        if not isinstance(raw_control, Mapping):
            continue
        control = _canonical_control(raw_control)
        if control is None or control["name"] in seen_names:
            continue
        seen_names.add(control["name"])
        controls.append(control)
    controls.sort(key=lambda control: control["name"])

    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "category_id": _as_int(source.get("category_id")),
        "classifier_id": _as_int(source.get("classifier_id")),
        "lang": _as_text(source.get("lang")) or "ru",
        "controls": controls,
    }


def attribute_manifest_hash(manifest: Mapping[str, Any] | None) -> str:
    """Hash the normalized transport contract, excluding volatile HTML data."""

    return _sha256(canonicalize_attribute_manifest(manifest))


def attribute_selection_hash(selection: Mapping[str, Any] | None) -> str:
    """Hash a normalized selection using a deterministic JSON encoding."""

    payload = dict(selection) if isinstance(selection, Mapping) else {}
    return _sha256(payload)


def normalize_attribute_selection(
    manifest: Mapping[str, Any] | None,
    selection: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Normalize a selection against one canonical manifest snapshot.

    Unknown fields, unavailable IDs, disabled options, and selections from
    inactive dynamic fragments are discarded.  Required fields are checked
    only while their parent option is active.
    """

    contract = canonicalize_attribute_manifest(manifest)
    controls = contract["controls"]
    submitted = dict(selection) if isinstance(selection, Mapping) else {}
    controls_by_name = {control["name"]: control for control in controls}
    issues: dict[str, list[Any]] = {
        "unknown_fields": [],
        "invalid_option_ids": [],
        "disabled_option_ids": [],
        "truncated_fields": [],
        "invalidated_children": [],
        "disabled_fields": [],
    }
    normalized: dict[str, Any] = {}

    for name in submitted:
        if str(name) not in controls_by_name:
            issues["unknown_fields"].append(str(name))

    for control in controls:
        name = control["name"]
        raw_value = submitted.get(name)
        if raw_value in (None, "") or (isinstance(raw_value, Sequence) and not isinstance(raw_value, str) and not raw_value):
            continue
        if control["disabled"]:
            issues["disabled_fields"].append(name)
            continue

        options = control["options"]
        if not options:
            text = _as_text(raw_value)
            if text:
                normalized[name] = text
            continue

        requested = _as_id_values(raw_value)
        option_by_id = {option["id"]: option for option in options}
        requested_ids = set(requested)
        for option_id in requested:
            option = option_by_id.get(option_id)
            if option is None:
                issues["invalid_option_ids"].append({"field": name, "id": option_id})
            elif option["disabled"]:
                issues["disabled_option_ids"].append({"field": name, "id": option_id})

        accepted = [option["id"] for option in options if option["id"] in requested_ids and not option["disabled"]]
        if not control["multiple"] and len(accepted) > 1:
            issues["truncated_fields"].append(name)
            accepted = accepted[:1]
        if accepted:
            normalized[name] = accepted if control["multiple"] else accepted[0]

    # A child fragment is valid only while at least one option which loaded it
    # remains selected.  Repeat because invalidating a parent can orphan deeper
    # descendants in the same dynamic form tree.
    while True:
        selected_ids = {
            option_id
            for value in normalized.values()
            for option_id in _as_id_values(value)
        }
        invalidated: list[str] = []
        for control in controls:
            name = control["name"]
            parent_ids = control["parent_option_ids"]
            if name in normalized and parent_ids and not selected_ids.intersection(parent_ids):
                normalized.pop(name, None)
                invalidated.append(name)
        if not invalidated:
            break
        issues["invalidated_children"].extend(invalidated)

    selected_ids = {
        option_id
        for value in normalized.values()
        for option_id in _as_id_values(value)
    }
    unresolved_required: list[str] = []
    active_controls: list[str] = []
    for control in controls:
        parent_ids = control["parent_option_ids"]
        active = not control["disabled"] and (not parent_ids or bool(selected_ids.intersection(parent_ids)))
        if not active:
            continue
        active_controls.append(control["name"])
        if control["required"] and control["name"] not in normalized:
            unresolved_required.append(control["name"])

    clean_issues = {key: value for key, value in issues.items() if value}
    return {
        "manifest": contract,
        "manifest_hash": attribute_manifest_hash(contract),
        "selection": normalized,
        "selection_hash": attribute_selection_hash(normalized),
        "unresolved_required": unresolved_required,
        "active_controls": active_controls,
        "issues": clean_issues,
        "valid": not unresolved_required,
        "clean": not clean_issues,
    }
