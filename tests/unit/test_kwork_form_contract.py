from __future__ import annotations

from src.platforms.kwork_form_contract import (
    attribute_manifest_hash,
    normalize_attribute_selection,
)


def _manifest() -> dict[str, object]:
    return {
        "category_id": 41,
        "classifier_id": 3587,
        "lang": "ru",
        "controls": [
            {
                "name": "attribute[208]",
                "group_id": 208,
                "type": "radio",
                "required": True,
                "options": [
                    {"id": 3587, "label": "Bots", "has_child": True},
                    {"id": 9999, "label": "Other"},
                ],
            },
            {
                "name": "attribute[3610][]",
                "group_id": 3610,
                "type": "checkbox",
                "multiple": True,
                "required": True,
                "parent_option_ids": [3587],
                "options": [
                    {"id": 3612, "label": "Telegram"},
                    {"id": 5273361, "label": "Disabled", "disabled": True},
                ],
            },
        ],
    }


def test_normalizer_removes_untrusted_ids_and_keeps_only_active_children():
    result = normalize_attribute_selection(
        _manifest(),
        {
            "unknown[field]": 123,
            "attribute[208]": [9999, 3587],
            "attribute[3610][]": [3612, 5273361, 999999],
        },
    )

    assert result["selection"] == {
        "attribute[208]": 3587,
        "attribute[3610][]": [3612],
    }
    assert result["valid"] is True
    assert result["issues"]["unknown_fields"] == ["unknown[field]"]
    assert result["issues"]["disabled_option_ids"] == [{"field": "attribute[3610][]", "id": 5273361}]
    assert result["issues"]["invalid_option_ids"] == [{"field": "attribute[3610][]", "id": 999999}]
    assert result["issues"]["truncated_fields"] == ["attribute[208]"]


def test_parent_change_invalidates_child_and_required_fields_follow_active_fragment():
    result = normalize_attribute_selection(
        _manifest(),
        {
            "attribute[208]": 9999,
            "attribute[3610][]": [3612],
        },
    )

    assert result["selection"] == {"attribute[208]": 9999}
    assert result["issues"]["invalidated_children"] == ["attribute[3610][]"]
    assert result["unresolved_required"] == []
    assert result["valid"] is True


def test_manifest_hash_is_stable_for_equivalent_fragment_order():
    original = _manifest()
    reordered = dict(original)
    reordered["controls"] = list(reversed(original["controls"]))

    assert attribute_manifest_hash(original) == attribute_manifest_hash(reordered)
