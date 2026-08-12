"""Capability boundary for Buyer Search discovery requests.

The wrapped Kwork client is intentionally private.  Discovery workflows receive
only this object, which exposes audited read operations and has no generic
request or mutation escape hatch.
"""

from __future__ import annotations

import inspect
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Mapping


class BuyerReadCapabilityError(RuntimeError):
    """Raised when the underlying account client lacks a required read method."""


class BuyerMutationForbiddenError(PermissionError):
    """Raised when a discovery workflow tries to access a mutation capability."""


@dataclass(frozen=True, slots=True)
class BuyerReadProvenance:
    """Identity evidence stored with each read request and observation."""

    worker_id: str
    account_registration_id: str
    transport_id: str
    egress_ip: str
    source: str

    def __post_init__(self) -> None:
        for name in ("worker_id", "account_registration_id", "transport_id", "egress_ip", "source"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} cannot be blank")

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


ReadMethod = Callable[..., Any | Awaitable[Any]]


class BuyerReadCapabilities:
    """Expose only explicit Buyer Search read operations for one worker identity."""

    _METHOD_ALIASES: Mapping[str, tuple[str, ...]] = {
        "projects": ("fetch_buyer_projects", "fetch_projects", "get_projects"),
        "project_detail": ("fetch_project_detail", "get_project_detail"),
        "want_detail": ("fetch_want_detail", "get_want_detail"),
        "buyer_history": ("fetch_buyer_history", "get_buyer_history"),
        "search_suggestions": ("fetch_want_search_suggestions", "get_search_suggestions"),
        "web_projects": ("fetch_web_projects", "get_web_projects"),
        "attachment": ("download_attachment", "fetch_attachment"),
    }
    _FORBIDDEN_PREFIXES = (
        "add_",
        "archive",
        "create_",
        "delete",
        "edit",
        "manage_",
        "mark_",
        "mutate",
        "publish",
        "restart",
        "save_",
        "send",
        "set_",
        "submit",
        "update_",
        "write",
    )

    def __init__(self, client: object, provenance: BuyerReadProvenance) -> None:
        self._client = client
        self.provenance = provenance

    async def fetch_projects(self, **params: Any) -> Any:
        return await self._call("projects", **params)

    async def fetch_project_detail(self, project_id: str | int, **params: Any) -> Any:
        return await self._call("project_detail", project_id, **params)

    async def fetch_want_detail(self, want_id: str | int, **params: Any) -> Any:
        return await self._call("want_detail", want_id, **params)

    async def fetch_buyer_history(self, buyer_id: str | int, **params: Any) -> Any:
        return await self._call("buyer_history", buyer_id, **params)

    async def fetch_search_suggestions(self, query: str, **params: Any) -> Any:
        return await self._call("search_suggestions", query, **params)

    async def fetch_web_projects(self, **params: Any) -> Any:
        return await self._call("web_projects", **params)

    async def download_attachment(self, attachment_url: str, **params: Any) -> Any:
        return await self._call("attachment", attachment_url, **params)

    async def _call(self, capability: str, *args: Any, **params: Any) -> Any:
        for method_name in self._METHOD_ALIASES[capability]:
            method = getattr(self._client, method_name, None)
            if callable(method):
                value = method(*args, **params)
                return await value if inspect.isawaitable(value) else value
        available = ", ".join(self._METHOD_ALIASES[capability])
        raise BuyerReadCapabilityError(f"client does not provide Buyer Search read capability {capability!r}: {available}")

    def __getattr__(self, name: str) -> Any:
        if name.startswith(self._FORBIDDEN_PREFIXES):
            raise BuyerMutationForbiddenError(f"discovery capability cannot access mutation method {name!r}")
        raise AttributeError(name)


__all__ = [
    "BuyerMutationForbiddenError",
    "BuyerReadCapabilities",
    "BuyerReadCapabilityError",
    "BuyerReadProvenance",
]
