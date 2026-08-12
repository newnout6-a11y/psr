"""Account-bound, read-only Kwork catalog capture for Buyer taxonomy.

The local taxonomy service remains transport-free.  This controller borrows an
already leased Buyer identity, reads a bounded catalog snapshot through its
account and VPNTE route, then commits exactly one immutable local snapshot.
It never exposes a generic Kwork client or a remote mutation method.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
import inspect
from typing import Any, Protocol, runtime_checkable

from .service import BuyerSearchSettings
from .sources.capabilities import BuyerReadProvenance
from .taxonomy import BuyerTaxonomyError, BuyerTaxonomyService


class BuyerTaxonomyRefreshError(ValueError):
    """Raised when an account-bound taxonomy capture cannot be committed."""


class BuyerTaxonomyRefreshDisabledError(BuyerTaxonomyRefreshError):
    """Raised while remote taxonomy refresh is disabled by feature flag."""


class BuyerTaxonomyRefreshCapabilityError(BuyerTaxonomyRefreshError):
    """Raised when a catalog reader loses its required account boundary."""


@runtime_checkable
class BuyerTaxonomyCatalogClient(Protocol):
    """The only remote catalog reads permitted during taxonomy capture."""

    def fetch_catalog_rubrics(self) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        """Read catalog root rubrics without changing remote state."""

    def fetch_catalog_categories(
        self,
        *,
        rubric_id: int,
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        """Read categories scoped to one catalog rubric."""

    def fetch_category_attributes(
        self,
        *,
        category_id: int,
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        """Read one category's available attributes."""

    def fetch_catalog_filters(
        self,
        *,
        category_id: int,
    ) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        """Read one category's available catalog filters."""

    def fetch_catalog_main(self) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        """Read global catalog seed data without a write operation."""


class BuyerTaxonomyReadCapabilities:
    """Private-client wrapper exposing only bounded account-bound catalog reads."""

    def __init__(
        self,
        client: BuyerTaxonomyCatalogClient | object,
        provenance: BuyerReadProvenance,
        *,
        release: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if not isinstance(provenance, BuyerReadProvenance):
            raise TypeError("provenance must be BuyerReadProvenance")
        self._client = client
        self.provenance = provenance
        self._release = release
        self._closed = False

    async def fetch_rubrics(self) -> dict[str, Any]:
        return await self._call("fetch_catalog_rubrics")

    async def fetch_categories(self, *, rubric_id: int) -> dict[str, Any]:
        return await self._call("fetch_catalog_categories", rubric_id=rubric_id)

    async def fetch_attributes(self, *, category_id: int) -> dict[str, Any]:
        return await self._call("fetch_category_attributes", category_id=category_id)

    async def fetch_filters(self, *, category_id: int) -> dict[str, Any]:
        return await self._call("fetch_catalog_filters", category_id=category_id)

    async def fetch_catalog_main(self) -> dict[str, Any]:
        return await self._call("fetch_catalog_main")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self._client, "close", None)
            if callable(close):
                value = close()
                if inspect.isawaitable(value):
                    await value
        finally:
            if self._release is not None:
                await self._release()

    async def _call(self, method_name: str, **kwargs: Any) -> dict[str, Any]:
        method = getattr(self._client, method_name, None)
        if not callable(method):
            raise BuyerTaxonomyRefreshCapabilityError(
                f"taxonomy reader does not expose read capability {method_name!r}"
            )
        value = method(**kwargs)
        value = await value if inspect.isawaitable(value) else value
        if not isinstance(value, Mapping):
            raise BuyerTaxonomyRefreshCapabilityError(
                f"taxonomy reader {method_name!r} returned an invalid payload"
            )
        return dict(value)


BuyerTaxonomyReadFactory = Callable[
    [str, str],
    BuyerTaxonomyReadCapabilities | Awaitable[BuyerTaxonomyReadCapabilities],
]


class BuyerTaxonomyRefreshController:
    """Collect and persist one bounded, account/run-scoped taxonomy snapshot."""

    def __init__(
        self,
        taxonomy_service: BuyerTaxonomyService,
        *,
        reader_factory: BuyerTaxonomyReadFactory,
        settings: BuyerSearchSettings,
    ) -> None:
        if not isinstance(taxonomy_service, BuyerTaxonomyService):
            raise TypeError("taxonomy_service must be BuyerTaxonomyService")
        if not callable(reader_factory):
            raise TypeError("reader_factory must be callable")
        if not isinstance(settings, BuyerSearchSettings):
            raise TypeError("settings must be BuyerSearchSettings")
        self._taxonomy_service = taxonomy_service
        self._reader_factory = reader_factory
        self._settings = settings

    async def refresh(
        self,
        *,
        run_id: str,
        account_registration_id: str,
        rubric_ids: Sequence[int] = (),
        category_ids: Sequence[int] = (),
        rubric_limit: int = 20,
        category_limit: int = 80,
        include_attributes: bool = True,
        include_filters: bool = True,
        catalog_seed_limit: int = 80,
    ) -> dict[str, Any]:
        """Capture remote catalog data before one immutable SQLite commit."""

        if not self._settings.taxonomy_refresh:
            raise BuyerTaxonomyRefreshDisabledError("Buyer taxonomy refresh is disabled by feature flag")
        normalized_run_id = _required_text(run_id, "run_id")
        account_id = _required_text(account_registration_id, "account_registration_id")
        normalized_rubric_ids = _positive_ids(rubric_ids, "rubric_ids", maximum=20)
        normalized_category_ids = _positive_ids(category_ids, "category_ids", maximum=80)
        normalized_rubric_limit = _bounded_limit(rubric_limit, "rubric_limit", minimum=1, maximum=20)
        normalized_category_limit = _bounded_limit(category_limit, "category_limit", minimum=1, maximum=80)
        normalized_seed_limit = _bounded_limit(catalog_seed_limit, "catalog_seed_limit", minimum=0, maximum=200)
        if not isinstance(include_attributes, bool) or not isinstance(include_filters, bool):
            raise BuyerTaxonomyRefreshError("include_attributes and include_filters must be booleans")

        reader: BuyerTaxonomyReadCapabilities | None = None
        try:
            reader = await _await_reader(self._reader_factory, normalized_run_id, account_id)
            _validate_reader_scope(reader, account_id)
            rubrics_payload = await reader.fetch_rubrics()
            rubrics = _rubrics_from_payload(rubrics_payload)
            selected_rubrics = _select_rubrics(
                rubrics,
                requested_ids=normalized_rubric_ids,
                limit=normalized_rubric_limit,
            )
            if len(selected_rubrics) > normalized_category_limit:
                raise BuyerTaxonomyRefreshError("category_limit must cover every selected rubric root")
            collector = _CategoryCollector(limit=normalized_category_limit)
            for rubric in selected_rubrics:
                collector.add(_root_category(rubric))
            for rubric in selected_rubrics:
                category_payload = await reader.fetch_categories(rubric_id=rubric["category_id"])
                collector.add_many(
                    _flatten_catalog_categories(
                        _payload_items(category_payload, "categories", "items", "data", "response"),
                        rubric_id=rubric["category_id"],
                        parent_category_id=rubric["category_id"],
                        parent_path=(rubric["category_id"],),
                    )
                )
            categories = collector.items
            descriptor_category_ids = _descriptor_category_ids(categories, normalized_category_ids)

            attributes: list[dict[str, Any]] = []
            filters: list[dict[str, Any]] = []
            for category_id in descriptor_category_ids:
                if include_attributes:
                    attributes_payload = await reader.fetch_attributes(category_id=category_id)
                    attributes.extend(_attribute_descriptors(attributes_payload, category_id=category_id))
                if include_filters:
                    filters_payload = await reader.fetch_filters(category_id=category_id)
                    filters.extend(_filter_descriptors(filters_payload, category_id=category_id))

            catalog_seeds: list[dict[str, Any]] = []
            if normalized_seed_limit:
                catalog_payload = await reader.fetch_catalog_main()
                catalog_seeds = _catalog_seed_terms(
                    catalog_payload,
                    category_ids={item["category_id"] for item in categories},
                    limit=normalized_seed_limit,
                )

            provenance = {
                **reader.provenance.as_dict(),
                "run_id": normalized_run_id,
                "endpoints": _captured_endpoints(
                    include_attributes=include_attributes,
                    include_filters=include_filters,
                    include_catalog_main=bool(normalized_seed_limit),
                ),
            }
            snapshot = await self._taxonomy_service.record_snapshot(
                {
                    "source": f"kwork-catalog:{account_id}",
                    "captured_at": _utc_now(),
                    "provenance": provenance,
                    "categories": categories,
                    "attributes": attributes,
                    "filters": filters,
                    "catalog_seeds": catalog_seeds,
                }
            )
            return {
                "snapshot": snapshot,
                "provenance": provenance,
                "category_count": snapshot["category_count"],
                "attribute_count": snapshot["attribute_count"],
                "filter_count": snapshot["filter_count"],
                "term_count": snapshot["term_count"],
                "remote_write": False,
            }
        except (BuyerTaxonomyRefreshError, BuyerTaxonomyError):
            raise
        except Exception as exc:  # noqa: BLE001 - preserve one safe API boundary for remote and normalization failures.
            raise BuyerTaxonomyRefreshError(f"Buyer taxonomy refresh failed: {exc}") from exc
        finally:
            if reader is not None:
                await reader.close()


async def _await_reader(
    factory: BuyerTaxonomyReadFactory,
    run_id: str,
    account_registration_id: str,
) -> BuyerTaxonomyReadCapabilities:
    value = factory(run_id, account_registration_id)
    value = await value if inspect.isawaitable(value) else value
    if not isinstance(value, BuyerTaxonomyReadCapabilities):
        raise BuyerTaxonomyRefreshCapabilityError("reader_factory must return BuyerTaxonomyReadCapabilities")
    return value


def _validate_reader_scope(reader: BuyerTaxonomyReadCapabilities, account_registration_id: str) -> None:
    if reader.provenance.account_registration_id != account_registration_id:
        raise BuyerTaxonomyRefreshCapabilityError(
            "account-bound taxonomy reader does not match account_registration_id"
        )
    if reader.provenance.source != "taxonomy_refresh":
        raise BuyerTaxonomyRefreshCapabilityError("taxonomy reader provenance must use source='taxonomy_refresh'")


def _rubrics_from_payload(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in _payload_items(payload, "rubrics", "items", "data", "categories", "response"):
        category_id = _positive_int(_first_value(raw, "rubric_id", "rubricId", "category_id", "categoryId", "id"), "rubric_id")
        if category_id in seen:
            continue
        seen.add(category_id)
        result.append(
            {
                "category_id": category_id,
                "name": _required_text(_first_value(raw, "name", "title"), "rubric name"),
                "raw": raw,
            }
        )
    if not result:
        raise BuyerTaxonomyRefreshError("catalogRubrics returned no usable rubrics")
    return result


def _select_rubrics(
    rubrics: Sequence[Mapping[str, Any]],
    *,
    requested_ids: tuple[int, ...],
    limit: int,
) -> list[dict[str, Any]]:
    available = {int(item["category_id"]): dict(item) for item in rubrics}
    if requested_ids:
        missing = [item for item in requested_ids if item not in available]
        if missing:
            raise BuyerTaxonomyRefreshError(f"requested rubric_ids are absent from catalogRubrics: {missing}")
        return [available[item] for item in requested_ids]
    return [dict(item) for item in rubrics[:limit]]


def _root_category(rubric: Mapping[str, Any]) -> dict[str, Any]:
    raw = rubric.get("raw") if isinstance(rubric.get("raw"), Mapping) else {}
    return {
        **dict(raw),
        "category_id": rubric["category_id"],
        "name": rubric["name"],
        "parent_category_id": None,
        "rubric_id": rubric["category_id"],
        "category_path": [rubric["category_id"]],
    }


def _flatten_catalog_categories(
    values: Sequence[Mapping[str, Any]],
    *,
    rubric_id: int,
    parent_category_id: int,
    parent_path: tuple[int, ...],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in values:
        category_id = _positive_int(_first_value(raw, "category_id", "categoryId", "id"), "category_id")
        explicit_parent = _positive_int_or_none(_first_value(raw, "parent_category_id", "parent_id", "parentId"))
        path = (*parent_path, category_id)
        category = {
            **dict(raw),
            "category_id": category_id,
            "name": _required_text(_first_value(raw, "name", "title"), "category name"),
            "parent_category_id": explicit_parent if explicit_parent is not None else parent_category_id,
            "rubric_id": _positive_int_or_none(_first_value(raw, "rubric_id", "rubricId")) or rubric_id,
            "classifier_id": _positive_int_or_none(_first_value(raw, "classifier_id", "classifierId")),
            "category_path": list(path),
        }
        result.append(category)
        children = _child_categories(raw)
        if children:
            result.extend(
                _flatten_catalog_categories(
                    children,
                    rubric_id=rubric_id,
                    parent_category_id=category_id,
                    parent_path=path,
                )
            )
    return result


class _CategoryCollector:
    """Preserve stable category identities while enforcing the remote-read cap."""

    def __init__(self, *, limit: int) -> None:
        self.limit = limit
        self._items: dict[int, dict[str, Any]] = {}

    @property
    def items(self) -> list[dict[str, Any]]:
        return list(self._items.values())

    def add_many(self, values: Sequence[Mapping[str, Any]]) -> None:
        for value in values:
            self.add(value)

    def add(self, value: Mapping[str, Any]) -> bool:
        category_id = _positive_int(value.get("category_id"), "category_id")
        candidate = dict(value)
        existing = self._items.get(category_id)
        if existing is not None:
            if str(existing.get("name") or "").strip().casefold() != str(candidate.get("name") or "").strip().casefold():
                raise BuyerTaxonomyRefreshError(f"catalog returned conflicting category names for ID {category_id}")
            return True
        if len(self._items) >= self.limit:
            return False
        self._items[category_id] = candidate
        return True


def _descriptor_category_ids(categories: Sequence[Mapping[str, Any]], requested_ids: tuple[int, ...]) -> tuple[int, ...]:
    available = {int(category["category_id"]) for category in categories}
    if requested_ids:
        missing = [item for item in requested_ids if item not in available]
        if missing:
            raise BuyerTaxonomyRefreshError(f"requested category_ids are absent from captured taxonomy: {missing}")
        return requested_ids
    return tuple(int(category["category_id"]) for category in categories)


def _attribute_descriptors(payload: Mapping[str, Any], *, category_id: int) -> list[dict[str, Any]]:
    return _descriptors_from_items(
        _payload_items(payload, "attributes", "items", "data", "response"),
        category_id=category_id,
        kind="attribute",
    )


def _filter_descriptors(payload: Mapping[str, Any], *, category_id: int) -> list[dict[str, Any]]:
    values = _payload_items(payload, "filters", "items", "data", "response")
    if values:
        return _descriptors_from_items(values, category_id=category_id, kind="filter")
    response = _response_mapping(payload)
    candidates: list[dict[str, Any]] = []
    for key, value in response.items():
        if key in {"success", "status", "code", "message"}:
            continue
        if isinstance(value, Mapping):
            candidate = dict(value)
            candidate.setdefault("filter_id", key)
            candidate.setdefault("name", key)
            candidate.setdefault("values", value.get("values") or value.get("options") or [dict(value)])
        elif _is_sequence(value):
            candidate = {"filter_id": key, "name": key, "values": list(value)}
        else:
            candidate = {"filter_id": key, "name": key, "values": [value]}
        candidates.append(candidate)
    return _descriptors_from_items(candidates, category_id=category_id, kind="filter")


def _descriptors_from_items(
    values: Sequence[Mapping[str, Any]],
    *,
    category_id: int,
    kind: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in values:
        descriptor_id = _first_value(raw, f"{kind}_id", "id", "key", "code", "name", "title")
        if descriptor_id is None:
            raise BuyerTaxonomyRefreshError(f"{kind} payload requires an id, key, code, or name")
        name = _first_value(raw, "name", "title") or descriptor_id
        result.append(
            {
                **dict(raw),
                "category_id": category_id,
                f"{kind}_id": str(descriptor_id),
                "name": _required_text(name, f"{kind} name"),
                "values": _descriptor_values(raw),
            }
        )
    return result


def _descriptor_values(raw: Mapping[str, Any]) -> list[Any]:
    for key in ("values", "options", "children", "childs"):
        if key not in raw or raw[key] is None:
            continue
        value = raw[key]
        if _is_sequence(value):
            return list(value)
        if isinstance(value, Mapping):
            return [dict(value)]
        return [value]
    return []


def _catalog_seed_terms(
    payload: Mapping[str, Any],
    *,
    category_ids: set[int],
    limit: int,
) -> list[dict[str, Any]]:
    terms: list[dict[str, Any]] = []
    seen: set[tuple[int | None, str]] = set()
    for raw in _walk_mappings(_response_mapping(payload), maximum=2_000):
        text = _optional_text(_first_value(raw, "text", "query", "keyword", "name", "title"))
        if text is None:
            continue
        remote_category_id = _positive_int_or_none(_first_value(raw, "category_id", "categoryId"))
        category_id = remote_category_id if remote_category_id in category_ids else None
        identity = (category_id, text.casefold())
        if identity in seen:
            continue
        seen.add(identity)
        metadata = dict(raw)
        if remote_category_id is not None and category_id is None:
            metadata["remote_category_id"] = remote_category_id
        terms.append(
            {
                "kind": "catalog_seed",
                "category_id": category_id,
                "text": text,
                "metadata": metadata,
            }
        )
        if len(terms) >= limit:
            break
    return terms


def _payload_items(payload: Mapping[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        if key not in payload or payload[key] is None:
            continue
        value = payload[key]
        if _is_sequence(value):
            return _mapping_sequence(value, key)
        if isinstance(value, Mapping):
            nested = _payload_items(value, "items", "data", "rubrics", "categories", "attributes", "filters", "response")
            if nested:
                return nested
    return []


def _response_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    response = payload.get("response")
    return dict(response) if isinstance(response, Mapping) else dict(payload)


def _mapping_sequence(value: Sequence[Any], name: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise BuyerTaxonomyRefreshError(f"catalog payload {name!r} contains a non-object item")
        items.append(dict(item))
    return items


def _child_categories(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    for key in ("children", "childs", "categories", "subcategories"):
        value = raw.get(key)
        if _is_sequence(value):
            return _mapping_sequence(value, key)
    return []


def _walk_mappings(value: Any, *, maximum: int) -> Sequence[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    stack: list[Any] = [value]
    while stack and len(result) < maximum:
        current = stack.pop()
        if isinstance(current, Mapping):
            result.append(current)
            stack.extend(current.values())
        elif _is_sequence(current):
            stack.extend(current)
    return result


def _captured_endpoints(
    *,
    include_attributes: bool,
    include_filters: bool,
    include_catalog_main: bool,
) -> list[str]:
    endpoints = ["catalogRubrics", "catalogCategories"]
    if include_attributes:
        endpoints.append("categoryAttributes")
    if include_filters:
        endpoints.append("catalogFilters")
    if include_catalog_main:
        endpoints.append("catalogMainv2")
    return endpoints


def _positive_ids(value: Sequence[int], name: str, *, maximum: int) -> tuple[int, ...]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise BuyerTaxonomyRefreshError(f"{name} must be an array of positive integers")
    result: list[int] = []
    seen: set[int] = set()
    for item in value:
        normalized = _positive_int(item, name)
        if normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    if len(result) > maximum:
        raise BuyerTaxonomyRefreshError(f"{name} cannot contain more than {maximum} values")
    return tuple(result)


def _bounded_limit(value: object, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise BuyerTaxonomyRefreshError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise BuyerTaxonomyRefreshError(f"{name} must be an integer") from exc
    if not minimum <= result <= maximum:
        raise BuyerTaxonomyRefreshError(f"{name} must be between {minimum} and {maximum}")
    return result


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise BuyerTaxonomyRefreshError(f"{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise BuyerTaxonomyRefreshError(f"{name} must be a positive integer") from exc
    if result <= 0:
        raise BuyerTaxonomyRefreshError(f"{name} must be a positive integer")
    return result


def _positive_int_or_none(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return _positive_int(value, "category_id")
    except BuyerTaxonomyRefreshError:
        return None


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise BuyerTaxonomyRefreshError(f"{name} must be a string")
    text = value.strip()
    if not text:
        raise BuyerTaxonomyRefreshError(f"{name} cannot be blank")
    return text


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_value(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "BuyerTaxonomyCatalogClient",
    "BuyerTaxonomyReadCapabilities",
    "BuyerTaxonomyReadFactory",
    "BuyerTaxonomyRefreshCapabilityError",
    "BuyerTaxonomyRefreshController",
    "BuyerTaxonomyRefreshDisabledError",
    "BuyerTaxonomyRefreshError",
]
