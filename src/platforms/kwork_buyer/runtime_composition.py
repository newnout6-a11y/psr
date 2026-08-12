"""Server-side composition helpers for live Buyer Search discovery.

This module deliberately keeps two integration concerns outside the Buyer
domain service:

* every Buyer run receives a deterministic, durable ``MarketJob`` used solely
  for account and VPNTE lease ownership;
* every account-bound read client is constructed from that worker's account
  context and its currently healthy VPNTE route.

Neither helper starts a supply workflow, creates Market operations, uses a
global Kwork service, or falls back to a direct network connection.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
import inspect
import json
from typing import Any, Protocol, TypeAlias
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

from src.platforms.kwork_supply.identity_pool import MarketAccountContext, MarketIdentityPool
from src.platforms.kwork_supply.models import (
    JobKind,
    JobState,
    MarketJobCreate,
    MarketScope,
    NetworkPolicy,
    SourcePolicy,
    TransportHealth,
    TransportKind,
    TransportSnapshot,
)
from src.platforms.kwork_supply.repository import MarketJobRepository
from src.platforms.kwork_supply.transports.vpnte import VpnteTransportManager

from .conversation_sync import BuyerConversationReadCapabilities, BuyerConversationSyncCapabilityError
from .taxonomy_refresh import BuyerTaxonomyReadCapabilities, BuyerTaxonomyRefreshCapabilityError
from .worker import BuyerDiscoveryIdentity
from .sources.capabilities import BuyerReadCapabilities, BuyerReadProvenance


BUYER_BACKING_JOB_PREFIX = "buyer-search"
BUYER_BACKING_SCOPE_CATEGORY_ID = 1
BUYER_INBOX_HISTORY_MAX_PAGES = 50


class BuyerRuntimeCompositionError(RuntimeError):
    """Base error for unsafe live Buyer runtime composition."""


class BuyerBackingJobError(BuyerRuntimeCompositionError):
    """Raised when a Buyer run cannot be resolved to its dedicated Market job."""


class BuyerTransportUnavailableError(BuyerRuntimeCompositionError):
    """Raised when an account identity no longer owns a healthy VPNTE route."""


class BuyerAccountClientFactoryError(BuyerRuntimeCompositionError):
    """Raised when a client cannot be built from one account-bound identity."""


class BuyerAccountBoundKworkClientError(BuyerRuntimeCompositionError):
    """Read error carrying enough HTTP context for the Buyer worker retry policy."""

    def __init__(self, message: str, *, status_code: int | None = None, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class BuyerAttachmentCapabilitiesUnavailableError(BuyerRuntimeCompositionError):
    """Raised when no idle, account-bound Buyer worker can read an attachment."""


BuyerKworkClientBuilder: TypeAlias = Callable[
    [MarketAccountContext, BuyerDiscoveryIdentity, TransportSnapshot],
    object | Awaitable[object],
]


class _TransportManager(Protocol):
    """Minimal synchronous VPNTE manager surface used during composition."""

    def refresh(self) -> Sequence[TransportSnapshot]:
        """Refresh and return all currently discovered VPNTE routes."""

    def get(self, transport_id: str) -> TransportSnapshot | None:
        """Return one latest local route snapshot."""


class BuyerRunMarketJobResolver:
    """Create and resolve a dedicated Market control-plane job for a Buyer run.

    The deterministic identifier makes the association durable without adding a
    second mapping table.  The created job is intentionally only an identity
    lease container: no coordinator or supply operation is invoked here.
    """

    def __init__(
        self,
        repository: MarketJobRepository,
        *,
        job_id_prefix: str = BUYER_BACKING_JOB_PREFIX,
        fallback_category_id: int = BUYER_BACKING_SCOPE_CATEGORY_ID,
    ) -> None:
        for method_name in ("initialize", "create_job", "get_job"):
            if not callable(getattr(repository, method_name, None)):
                raise TypeError(f"repository must expose {method_name}")
        self.repository = repository
        self.job_id_prefix = _required_text(job_id_prefix, "job_id_prefix")
        self.fallback_category_id = _positive_int(fallback_category_id, "fallback_category_id")

    async def __call__(self, run_id: str) -> str:
        """Resolve a previously created backing job for adapter callbacks."""

        return await self.resolve(run_id)

    async def ensure_for_run(
        self,
        run: Mapping[str, Any],
        *,
        account_registration_ids: Sequence[str] | None = None,
    ) -> str:
        """Create the backing job once, then return its durable identifier."""

        if not isinstance(run, Mapping):
            raise TypeError("run must be a mapping")
        run_id = _required_text(run.get("run_id"), "run.run_id")
        job_id = self.job_id_for_run(run_id)
        await self.repository.initialize()

        existing = await self.repository.get_job(job_id)
        if existing is not None:
            self._validate_backing_job(existing, run_id)
            return job_id

        create = _backing_job_create(
            run,
            account_registration_ids=_normalize_registration_ids(
                account_registration_ids if account_registration_ids is not None else run.get("account_registration_ids")
            ),
            fallback_category_id=self.fallback_category_id,
        )
        try:
            created = await self.repository.create_job(create, job_id=job_id)
        except Exception:
            # A concurrent API request may have inserted the same deterministic
            # ID after the first read. Only accept that exact Buyer backing job.
            created = await self.repository.get_job(job_id)
            if created is None:
                raise
        self._validate_backing_job(created, run_id)
        return job_id

    async def resolve(self, run_id: str) -> str:
        """Return a verified backing job instead of guessing a run/job mapping."""

        normalized_run_id = _required_text(run_id, "run_id")
        job_id = self.job_id_for_run(normalized_run_id)
        job = await self.repository.get_job(job_id)
        if job is None:
            raise BuyerBackingJobError(f"Buyer run {normalized_run_id!r} has no durable backing Market job")
        self._validate_backing_job(job, normalized_run_id)
        return job_id

    async def finalize_for_run(self, run_id: str, reason: str = "buyer_run_terminal") -> str:
        """Stop the lease-only Market job when its Buyer run is terminal."""

        normalized_run_id = _required_text(run_id, "run_id")
        job_id = self.job_id_for_run(normalized_run_id)
        job = await self.repository.get_job(job_id)
        if job is None:
            raise BuyerBackingJobError(f"Buyer run {normalized_run_id!r} has no durable backing Market job")
        self._validate_backing_job(job, normalized_run_id)
        if str(job.get("state") or "") in {
            JobState.COMPLETED.value,
            JobState.STOPPED.value,
            JobState.FAILED.value,
        }:
            return job_id
        await self.repository.update_job_state(
            job_id,
            JobState.STOPPED,
            phase=str(job.get("phase") or "prepare"),
            expected_revision=int(job.get("revision") or 0),
            last_warning=_optional_text(reason) or "buyer_run_terminal",
        )
        return job_id

    def job_id_for_run(self, run_id: str) -> str:
        """Return the one deterministic Market job ID allowed for ``run_id``."""

        return f"{self.job_id_prefix}:{_required_text(run_id, 'run_id')}"

    @staticmethod
    def _validate_backing_job(job: Mapping[str, Any], run_id: str) -> None:
        if not isinstance(job, Mapping):
            raise BuyerBackingJobError("Market job repository returned an invalid backing job record")
        if str(job.get("job_kind") or "") != JobKind.BUYER_SEARCH.value:
            raise BuyerBackingJobError("backing Market job belongs to a non-Buyer workflow")
        workflow_config = job.get("workflow_config")
        if not isinstance(workflow_config, Mapping) or workflow_config.get("buyer_run_id") != run_id:
            raise BuyerBackingJobError("backing Market job does not belong to this Buyer run")
        if str(job.get("network_policy") or "") != NetworkPolicy.VPNTE_ONLY.value:
            raise BuyerBackingJobError("backing Market job does not enforce VPNTE-only networking")


class AccountBoundBuyerKworkClientFactory:
    """Build Buyer read clients only from an owned, refreshed VPNTE snapshot."""

    def __init__(
        self,
        transport_manager: VpnteTransportManager,
        *,
        client_builder: BuyerKworkClientBuilder | None = None,
    ) -> None:
        for method_name in ("refresh", "get"):
            if not callable(getattr(transport_manager, method_name, None)):
                raise TypeError(f"transport_manager must expose {method_name}")
        self.transport_manager: _TransportManager = transport_manager
        self.client_builder = client_builder or _build_default_account_bound_client

    async def __call__(self, context: MarketAccountContext, identity: BuyerDiscoveryIdentity) -> object:
        """Return a client tied to the exact leased account and VPNTE route."""

        _validate_account_identity(context, identity)
        snapshot = await asyncio.to_thread(self._refresh_and_validate_route, identity)
        client = self.client_builder(context, identity, snapshot)
        if inspect.isawaitable(client):
            client = await client
        if client is None:
            raise BuyerAccountClientFactoryError("account-bound Kwork client builder returned no client")
        return client

    def _refresh_and_validate_route(self, identity: BuyerDiscoveryIdentity) -> TransportSnapshot:
        try:
            refreshed = tuple(self.transport_manager.refresh())
        except Exception as exc:  # noqa: BLE001 - no stale or direct route fallback is permitted.
            raise BuyerTransportUnavailableError(f"VPNTE refresh failed for Buyer worker {identity.worker_id!r}") from exc

        snapshot = self.transport_manager.get(identity.transport_id)
        if snapshot is None:
            snapshot = next(
                (item for item in refreshed if isinstance(item, TransportSnapshot) and item.transport_id == identity.transport_id),
                None,
            )
        return _validate_route_snapshot(snapshot, identity)


class AccountBoundBuyerKworkClient:
    """Small read-only Kwork facade bound to one explicit account and proxy.

    It deliberately creates its own API/web clients with ``trust_env=False``
    and the VPNTE proxy supplied by the verified snapshot. No environment or
    shared-session route can replace that proxy.
    """

    def __init__(
        self,
        context: MarketAccountContext,
        identity: BuyerDiscoveryIdentity,
        snapshot: TransportSnapshot,
    ) -> None:
        _validate_account_identity(context, identity)
        self.context = context
        self.identity = identity
        self.snapshot = _validate_route_snapshot(snapshot, identity)
        self.proxy_url = self.snapshot.proxy_url or ""
        if not self.proxy_url:
            raise BuyerTransportUnavailableError("Buyer account client cannot be built without a VPNTE proxy URL")

        # Importing this direct client is safe: it has no Session Hub calls and
        # uses the explicit ``proxy_url`` below for every mobile API request.
        from src.platforms.kwork_market import KworkMarketClient

        self._mobile_client = KworkMarketClient(
            proxy_url=self.proxy_url,
            use_environment_proxy=False,
            account_email=context.email,
            account_password=context.password,
            account_cookies=context.cookies,
            persona_headers=context.headers,
        )

    async def fetch_buyer_projects(self, **params: Any) -> Mapping[str, Any]:
        """Read the mobile ``projects`` endpoint with this account's API session."""

        request = {key: value for key, value in params.items() if value is not None}
        request.setdefault("page", 1)
        request.setdefault("query", "")
        request.setdefault("categories", "all")
        request["use_token"] = True
        try:
            response = await self._mobile_client.request("projects", **request)
        except Exception as exc:  # noqa: BLE001 - preserve transport status for worker retries when available.
            raise _as_account_client_error(exc, "Kwork mobile projects request failed") from exc
        if not isinstance(response, Mapping):
            raise BuyerAccountBoundKworkClientError("Kwork mobile projects response is not a mapping")
        return dict(response)

    async def fetch_want_detail(self, want_id: str | int, **params: Any) -> Mapping[str, Any]:
        """Read one mobile ``want`` record through the leased account session."""

        return await self._fetch_mobile_detail("want", want_id, **params)

    async def fetch_project_detail(self, project_id: str | int, **params: Any) -> Mapping[str, Any]:
        """Read one mobile ``project`` record through the leased account session."""

        return await self._fetch_mobile_detail("project", project_id, **params)

    async def fetch_buyer_history(self, buyer_id: str | int, **params: Any) -> Mapping[str, Any]:
        """Read the buyer's project history using the same account cookies and VPNTE route."""

        username = _required_text(str(buyer_id), "buyer_id").strip("/")
        page = _optional_positive_int(params.get("page")) or 1
        limit = min(_optional_positive_int(params.get("limit")) or 8, 50)
        headers = self._web_headers("PSR-BuyerSearchHistory/1.0")
        encoded_username = quote(username, safe="-_.~")
        try:
            async with httpx.AsyncClient(
                headers=headers,
                cookies=dict(self.context.cookies),
                timeout=12.0,
                follow_redirects=True,
                proxy=self.proxy_url,
                trust_env=False,
            ) as client:
                response = await client.get(
                    f"https://kwork.ru/projects/list/{encoded_username}",
                    params={"page": page},
                )
        except httpx.HTTPError as exc:
            raise _as_account_client_error(exc, "Kwork buyer history request failed") from exc
        self._raise_for_read_status(response, "Kwork buyer history")
        from src.platforms.kwork import KworkStateDataParser

        state = KworkStateDataParser.extract(response.text) or {}
        items = _web_state_items(state)[:limit]
        wants = state.get("wants") if isinstance(state.get("wants"), Mapping) else {}
        return {
            "username": username,
            "items": items,
            "page": page,
            "total": wants.get("total") or state.get("wantsCount") or len(items),
            "status_code": response.status_code,
            "url": str(response.url),
        }

    async def fetch_catalog_rubrics(self) -> Mapping[str, Any]:
        """Read Kwork catalog rubrics through the leased account API client."""

        return await self._fetch_mobile_catalog("catalogRubrics")

    async def fetch_catalog_categories(self, *, rubric_id: int) -> Mapping[str, Any]:
        """Read one Kwork catalog rubric using its required camelCase parameter."""

        return await self._fetch_mobile_catalog(
            "catalogCategories",
            rubricId=_positive_int(rubric_id, "rubric_id"),
        )

    async def fetch_category_attributes(self, *, category_id: int) -> Mapping[str, Any]:
        """Read immutable category attributes through the leased account API client."""

        return await self._fetch_mobile_catalog(
            "categoryAttributes",
            category_id=_positive_int(category_id, "category_id"),
        )

    async def fetch_catalog_filters(self, *, category_id: int) -> Mapping[str, Any]:
        """Read immutable catalog filters with Kwork's required camelCase key."""

        return await self._fetch_mobile_catalog(
            "catalogFilters",
            categoryId=_positive_int(category_id, "category_id"),
        )

    async def fetch_catalog_main(self) -> Mapping[str, Any]:
        """Read global catalog seed data through the same account and VPNTE route."""

        return await self._fetch_mobile_catalog("catalogMainv2")

    async def fetch_inbox_dialogs(self, *, cursor: str | None, limit: int) -> Mapping[str, Any]:
        """Read the authenticated web inbox through this account's leased route.

        Kwork's web inbox does not expose a verified pagination cursor yet, so
        the caller stores the newest observed watermark and this method always
        reads one bounded snapshot.  It never marks messages read or sends
        anything remotely.
        """

        _ = cursor
        bounded_limit = min(_positive_int(limit, "limit"), 200)
        headers = self._web_headers("PSR-BuyerConversationSync/1.0")
        headers["Referer"] = "https://kwork.ru/inbox"
        try:
            async with httpx.AsyncClient(
                headers=headers,
                cookies=dict(self.context.cookies),
                timeout=12.0,
                follow_redirects=True,
                proxy=self.proxy_url,
                trust_env=False,
            ) as client:
                response = await client.get("https://kwork.ru/inbox")
        except httpx.HTTPError as exc:
            raise _as_account_client_error(exc, "Kwork web inbox request failed") from exc
        self._raise_for_read_status(response, "Kwork web inbox")
        dialogs = [
            dialog
            for item in _extract_web_chat_list(response.text)
            if isinstance(item, Mapping)
            if (dialog := _normalize_web_inbox_dialog(item)) is not None
        ][:bounded_limit]
        return {
            "dialogs": dialogs,
            "watermark": _inbox_watermark(dialogs),
            "status_code": response.status_code,
            "url": str(response.url),
        }

    async def fetch_inbox_messages(
        self,
        *,
        dialog: Mapping[str, Any],
        cursor: str | None,
        limit: int,
    ) -> Mapping[str, Any]:
        """Read bounded existing history for one dialog through the mobile API."""

        username = _inbox_dialog_username(dialog)
        bounded_limit = min(_positive_int(limit, "limit"), 1_000)
        page = _optional_positive_int(cursor) or 1
        member_id = _optional_text(dialog.get("member_id"))
        messages: list[dict[str, Any]] = []
        pages = page
        last_allowed_page = page + BUYER_INBOX_HISTORY_MAX_PAGES - 1
        last_response: Mapping[str, Any] | None = None
        while page <= pages and page <= last_allowed_page and len(messages) < bounded_limit:
            try:
                response = await self._mobile_client.request(
                    "inboxes",
                    use_token=True,
                    username=username,
                    page=page,
                )
            except Exception as exc:  # noqa: BLE001 - preserve retryable transport status where available.
                raise _as_account_client_error(exc, "Kwork inbox history request failed") from exc
            if not isinstance(response, Mapping):
                raise BuyerAccountBoundKworkClientError("Kwork inbox history response is not a mapping")
            last_response = response
            page_messages = _inbox_message_items(response)
            if not page_messages:
                break
            messages.extend(_normalize_inbox_message(item, member_id=member_id) for item in page_messages)
            pages = _inbox_page_count(response, current_page=page)
            page += 1

        return {
            "messages": messages[:bounded_limit],
            "paging": {
                "page": page - 1,
                "pages": pages,
                "next_cursor": str(page)
                if page <= pages and (len(messages) >= bounded_limit or page > last_allowed_page)
                else None,
            },
            "status_code": last_response.get("status_code") if last_response is not None else None,
        }

    async def send_conversation_message(
        self,
        *,
        remote_dialog_id: str,
        body: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        """Send one explicitly confirmed inbox reply through this leased account.

        This is intentionally the only mutation on the account-bound Buyer
        client.  Its caller is the short-lived conversation-send capability;
        discovery and sync readers never call it.  Kwork's documented
        ``inboxCreate`` payload has no idempotency field, so the immutable
        local key is returned as durable receipt metadata rather than sent as
        an undocumented request parameter.
        """

        remote_user_id = _positive_int(remote_dialog_id, "remote_dialog_id")
        message_body = _required_text(body, "body")
        local_idempotency_key = _required_text(idempotency_key, "idempotency_key")
        try:
            get_api = getattr(self._mobile_client, "_get_api", None)
            if not callable(get_api):
                raise BuyerAccountBoundKworkClientError(
                    "Kwork mobile client does not expose the account-bound inboxCreate transport"
                )
            api = get_api()
            api = await api if inspect.isawaitable(api) else api
            request_with_body = getattr(api, "request_with_body", None)
            if not callable(request_with_body):
                raise BuyerAccountBoundKworkClientError(
                    "Kwork account transport does not expose inboxCreate request_with_body"
                )
            response = request_with_body(
                "inboxCreate",
                use_token=True,
                retry=False,
                body={"text": message_body},
                user_id=remote_user_id,
            )
            response = await response if inspect.isawaitable(response) else response
        except BuyerAccountBoundKworkClientError:
            raise
        except Exception as exc:  # noqa: BLE001 - caller records ambiguous remote outcomes without retrying.
            raise _as_account_client_error(exc, "Kwork inboxCreate request failed") from exc
        if not isinstance(response, Mapping):
            raise BuyerAccountBoundKworkClientError("Kwork inboxCreate response is not a mapping")
        return {
            "endpoint": "inboxCreate",
            "request": {"user_id": remote_user_id},
            "idempotency_key": local_idempotency_key,
            "remote_response": dict(response),
        }

    async def fetch_want_search_suggestions(self, query: str, **params: Any) -> Mapping[str, Any]:
        """Read Kwork's suggestion endpoint through the account-bound web session."""

        normalized_query = _required_text(query, "query")
        headers = self._web_headers("PSR-BuyerSearchSuggest/1.0")
        headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://kwork.ru",
                "Referer": "https://kwork.ru/projects",
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        try:
            async with httpx.AsyncClient(
                headers=headers,
                cookies=dict(self.context.cookies),
                timeout=12.0,
                follow_redirects=True,
                proxy=self.proxy_url,
                trust_env=False,
            ) as client:
                response = await client.post("https://kwork.ru/want-search/suggest", data={"query": normalized_query})
        except httpx.HTTPError as exc:
            raise _as_account_client_error(exc, "Kwork buyer suggestion request failed") from exc
        self._raise_for_read_status(response, "Kwork buyer suggestion")
        try:
            payload = response.json()
        except ValueError as exc:
            raise BuyerAccountBoundKworkClientError("Kwork buyer suggestion returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise BuyerAccountBoundKworkClientError("Kwork buyer suggestion response is not a mapping")
        return dict(payload)

    async def download_attachment(self, attachment_url: str, **params: Any) -> Mapping[str, Any]:
        """Stream one Kwork-hosted attachment through the exact leased VPNTE route."""

        url = _validate_kwork_asset_url(attachment_url)
        max_bytes = _bounded_download_bytes(params.get("max_bytes"))
        timeout = _bounded_read_timeout(params.get("timeout_seconds"))
        headers = self._web_headers("PSR-BuyerAttachment/1.0")
        chunks: list[bytes] = []
        received = 0
        try:
            async with httpx.AsyncClient(
                headers=headers,
                cookies=dict(self.context.cookies),
                timeout=timeout,
                follow_redirects=False,
                proxy=self.proxy_url,
                trust_env=False,
            ) as client:
                for redirect_count in range(4):
                    async with client.stream("GET", url) as response:
                        response_url = _validate_kwork_asset_url(str(response.url))
                        if 300 <= response.status_code < 400:
                            location = response.headers.get("location")
                            if not location:
                                raise BuyerAccountBoundKworkClientError(
                                    "Kwork attachment redirect is missing a Location header",
                                    status_code=response.status_code,
                                )
                            if redirect_count >= 3:
                                raise BuyerAccountBoundKworkClientError(
                                    "Kwork attachment exceeded the redirect limit",
                                    status_code=response.status_code,
                                )
                            # Validate each hop before any request leaves the leased route.
                            url = _validate_kwork_asset_url(urljoin(response_url, location))
                            continue
                        self._raise_for_read_status(response, "Kwork attachment download")
                        declared_size = _optional_positive_int(response.headers.get("content-length"))
                        if declared_size is not None and declared_size > max_bytes:
                            raise BuyerAccountBoundKworkClientError(
                                f"Kwork attachment exceeds {max_bytes} byte limit",
                                status_code=response.status_code,
                            )
                        async for chunk in response.aiter_bytes():
                            received += len(chunk)
                            if received > max_bytes:
                                raise BuyerAccountBoundKworkClientError(
                                    f"Kwork attachment exceeds {max_bytes} byte limit",
                                    status_code=response.status_code,
                                )
                            chunks.append(chunk)
                        filename = _filename_from_content_disposition(response.headers.get("content-disposition"))
                        return {
                            "content": b"".join(chunks),
                            "filename": filename,
                            "content_type": response.headers.get("content-type"),
                            "resolved_download_url": response_url,
                        }
            raise BuyerAccountBoundKworkClientError("Kwork attachment redirect limit exhausted")
        except BuyerAccountBoundKworkClientError:
            raise
        except httpx.HTTPError as exc:
            raise _as_account_client_error(exc, "Kwork attachment download failed") from exc

    async def _fetch_mobile_detail(self, endpoint: str, record_id: str | int, **params: Any) -> Mapping[str, Any]:
        normalized_id = _positive_int(record_id, f"{endpoint}_id")
        request = {key: value for key, value in params.items() if value is not None and key not in {"id", "use_token"}}
        request.update({"id": normalized_id, "use_token": True})
        try:
            response = await self._mobile_client.request(endpoint, **request)
        except Exception as exc:  # noqa: BLE001 - preserve transport status for worker retries when available.
            raise _as_account_client_error(exc, f"Kwork mobile {endpoint} request failed") from exc
        if not isinstance(response, Mapping):
            raise BuyerAccountBoundKworkClientError(f"Kwork mobile {endpoint} response is not a mapping")
        return dict(response)

    async def _fetch_mobile_catalog(self, endpoint: str, **params: Any) -> Mapping[str, Any]:
        """Use only the fixed read-only catalog endpoint set above."""

        request = {key: value for key, value in params.items() if value is not None}
        request["use_token"] = True
        try:
            response = await self._mobile_client.request(endpoint, **request)
        except Exception as exc:  # noqa: BLE001 - preserve transport status for retry/HTTP handling.
            raise _as_account_client_error(exc, f"Kwork mobile {endpoint} request failed") from exc
        if not isinstance(response, Mapping):
            raise BuyerAccountBoundKworkClientError(f"Kwork mobile {endpoint} response is not a mapping")
        return dict(response)

    def _web_headers(self, user_agent: str) -> dict[str, str]:
        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "User-Agent": user_agent,
        }
        headers.update({str(name): str(value) for name, value in self.context.headers.items() if str(name).strip()})
        return headers

    @staticmethod
    def _raise_for_read_status(response: httpx.Response, resource: str) -> None:
        if response.status_code < 400:
            return
        raise BuyerAccountBoundKworkClientError(
            f"{resource} returned HTTP {response.status_code}",
            status_code=response.status_code,
            retry_after_seconds=_retry_after_seconds(response.headers.get("retry-after")),
        )

    async def fetch_web_projects(self, **params: Any) -> Mapping[str, Any]:
        """Read authenticated web projects through the same explicit VPNTE proxy."""

        request = _web_project_params(params)
        headers = self._web_headers("Mozilla/5.0 PSR-BuyerSearch/1.0")
        try:
            async with httpx.AsyncClient(
                headers=headers,
                cookies=dict(self.context.cookies),
                timeout=12.0,
                follow_redirects=True,
                proxy=self.proxy_url,
                trust_env=False,
            ) as client:
                response = await client.get("https://kwork.ru/projects", params=request)
        except httpx.HTTPError as exc:
            raise _as_account_client_error(exc, "Kwork web projects request failed") from exc
        self._raise_for_read_status(response, "Kwork web projects")

        # This parser is a pure HTML-to-state helper. It is intentionally used
        # directly instead of going through the legacy shared Kwork service.
        from src.platforms.kwork import KworkStateDataParser

        state = KworkStateDataParser.extract(response.text) or {}
        items = _web_state_items(state)
        pagination = state.get("pagination") if isinstance(state.get("pagination"), Mapping) else {}
        return {
            "items": items,
            "paging": {
                "page": pagination.get("current_page") or request["page"],
                "total": pagination.get("total") or len(items),
                "pages": pagination.get("last_page") or pagination.get("pages"),
            },
            "status_code": response.status_code,
            "url": str(response.url),
        }

    async def close(self) -> None:
        """Close the locally-owned mobile API client when a caller retains it."""

        close = getattr(self._mobile_client, "close", None)
        if not callable(close):
            return
        result = close()
        if inspect.isawaitable(result):
            await result


class AccountBoundBuyerAttachmentCapabilities(BuyerReadCapabilities):
    """A short-lived read capability that closes its dedicated client on exit."""

    def __init__(
        self,
        client: object,
        provenance: BuyerReadProvenance,
        *,
        release: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(client, provenance)
        self._owned_client = client
        self._release = release
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self._owned_client, "close", None)
            if callable(close):
                value = close()
                if inspect.isawaitable(value):
                    await value
        finally:
            await self._release()


class AccountBoundBuyerAttachmentCapabilitiesFactory:
    """Borrow an idle discovery identity for one attachment-only read pass.

    The factory never allocates a second account or route.  It instead selects
    an already leased idle identity from the requested run through a
    supervisor-owned reservation. The discovery loop honours that reservation,
    so an attachment download and a query page cannot use the same
    account/route concurrently. This preserves account/route provenance
    without a global Kwork session or a direct-network fallback.
    """

    def __init__(
        self,
        supervisor: object,
        identity_pool: MarketIdentityPool,
        account_client_factory: AccountBoundBuyerKworkClientFactory,
    ) -> None:
        for method_name in ("reserve_attachment_identity", "release_attachment_identity"):
            if not callable(getattr(supervisor, method_name, None)):
                raise TypeError(f"supervisor must expose {method_name}")
        if not callable(getattr(identity_pool, "context_for_worker", None)):
            raise TypeError("identity_pool must expose context_for_worker")
        if not callable(account_client_factory):
            raise TypeError("account_client_factory must be callable")
        self.supervisor = supervisor
        self.identity_pool = identity_pool
        self.account_client_factory = account_client_factory

    async def __call__(
        self,
        run_id: str,
        project_id: str,
        account_registration_id: str,
    ) -> BuyerReadCapabilities:
        normalized_run_id = _required_text(run_id, "run_id")
        _required_text(project_id, "project_id")
        normalized_account_id = _required_text(account_registration_id, "account_registration_id")
        reservation = await _await_attachment_reservation(
            self.supervisor,
            normalized_run_id,
            normalized_account_id,
        )
        identity = _attachment_reservation_identity(reservation)
        client: object | None = None
        try:
            if identity.account_registration_id != normalized_account_id:
                raise BuyerAttachmentCapabilitiesUnavailableError(
                    "reserved Buyer worker does not match the requested account"
                )
            context = await self.identity_pool.context_for_worker(identity.worker_id)
            if context is None or context.registration_id != identity.account_registration_id:
                raise BuyerAttachmentCapabilitiesUnavailableError(
                    "idle Buyer worker no longer owns the requested account context"
                )
            client = await self.account_client_factory(context, identity)

            async def release() -> None:
                await _release_attachment_reservation(self.supervisor, reservation)

            return AccountBoundBuyerAttachmentCapabilities(
                client,
                identity.provenance("attachment_enrichment"),
                release=release,
            )
        except Exception:
            try:
                if client is not None:
                    close = getattr(client, "close", None)
                    if callable(close):
                        value = close()
                        if inspect.isawaitable(value):
                            await value
            finally:
                await _release_attachment_reservation(self.supervisor, reservation)
            raise


class AccountBoundBuyerConversationCapabilitiesFactory:
    """Borrow an active account/route lease for one read-only inbox sync.

    Unlike project attachments, conversations are not attached to one Buyer
    Search run.  The supervisor therefore finds the active fleet that already
    owns the requested account and reserves that exact worker identity.  The
    capability exposes inbox reads only and releases the reservation after the
    controller commits (or abandons) its SQLite batch.
    """

    def __init__(
        self,
        supervisor: object,
        identity_pool: MarketIdentityPool,
        account_client_factory: AccountBoundBuyerKworkClientFactory,
    ) -> None:
        for method_name in ("reserve_account_identity", "release_attachment_identity"):
            if not callable(getattr(supervisor, method_name, None)):
                raise TypeError(f"supervisor must expose {method_name}")
        if not callable(getattr(identity_pool, "context_for_worker", None)):
            raise TypeError("identity_pool must expose context_for_worker")
        if not callable(account_client_factory):
            raise TypeError("account_client_factory must be callable")
        self.supervisor = supervisor
        self.identity_pool = identity_pool
        self.account_client_factory = account_client_factory

    async def __call__(self, account_registration_id: str) -> BuyerConversationReadCapabilities:
        account_id = _required_text(account_registration_id, "account_registration_id")
        reservation = await _await_account_reservation(self.supervisor, account_id)
        identity = _conversation_reservation_identity(reservation)
        client: object | None = None
        try:
            if identity.account_registration_id != account_id:
                raise BuyerConversationSyncCapabilityError(
                    "reserved Buyer worker does not match the requested conversation account"
                )
            context = await self.identity_pool.context_for_worker(identity.worker_id)
            if context is None or context.registration_id != identity.account_registration_id:
                raise BuyerConversationSyncCapabilityError(
                    "idle Buyer worker no longer owns the requested account context"
                )
            client = await self.account_client_factory(context, identity)

            async def release() -> None:
                await _release_attachment_reservation(self.supervisor, reservation)

            return BuyerConversationReadCapabilities(
                client,
                identity.provenance("conversation_sync"),
                release=release,
            )
        except BuyerConversationSyncCapabilityError:
            try:
                if client is not None:
                    await _close_account_bound_client(client)
            finally:
                await _release_attachment_reservation(self.supervisor, reservation)
            raise
        except Exception as exc:  # noqa: BLE001 - preserve the narrow public capability boundary.
            try:
                if client is not None:
                    await _close_account_bound_client(client)
            finally:
                await _release_attachment_reservation(self.supervisor, reservation)
            raise BuyerConversationSyncCapabilityError(
                "requested account could not create an account-bound conversation reader"
            ) from exc


class AccountBoundBuyerTaxonomyCapabilitiesFactory:
    """Borrow one exact run/account lease for a read-only taxonomy capture.

    Taxonomy refresh names both a Buyer run and account, so it must not borrow
    an arbitrary global identity. The supervisor reservation pauses discovery
    for that exact worker until the bounded capture closes its client.
    """

    def __init__(
        self,
        supervisor: object,
        identity_pool: MarketIdentityPool,
        account_client_factory: AccountBoundBuyerKworkClientFactory,
    ) -> None:
        for method_name in ("reserve_attachment_identity", "release_attachment_identity"):
            if not callable(getattr(supervisor, method_name, None)):
                raise TypeError(f"supervisor must expose {method_name}")
        if not callable(getattr(identity_pool, "context_for_worker", None)):
            raise TypeError("identity_pool must expose context_for_worker")
        if not callable(account_client_factory):
            raise TypeError("account_client_factory must be callable")
        self.supervisor = supervisor
        self.identity_pool = identity_pool
        self.account_client_factory = account_client_factory

    async def __call__(
        self,
        run_id: str,
        account_registration_id: str,
    ) -> BuyerTaxonomyReadCapabilities:
        normalized_run_id = _required_text(run_id, "run_id")
        account_id = _required_text(account_registration_id, "account_registration_id")
        reservation = await _await_taxonomy_reservation(self.supervisor, normalized_run_id, account_id)
        identity = _taxonomy_reservation_identity(reservation)
        client: object | None = None
        try:
            if identity.account_registration_id != account_id:
                raise BuyerTaxonomyRefreshCapabilityError(
                    "reserved Buyer worker does not match the requested taxonomy account"
                )
            context = await self.identity_pool.context_for_worker(identity.worker_id)
            if context is None or context.registration_id != identity.account_registration_id:
                raise BuyerTaxonomyRefreshCapabilityError(
                    "idle Buyer worker no longer owns the requested account context"
                )
            client = await self.account_client_factory(context, identity)

            async def release() -> None:
                await _release_attachment_reservation(self.supervisor, reservation)

            return BuyerTaxonomyReadCapabilities(
                client,
                identity.provenance("taxonomy_refresh"),
                release=release,
            )
        except BuyerTaxonomyRefreshCapabilityError:
            try:
                if client is not None:
                    await _close_account_bound_client(client)
            finally:
                await _release_attachment_reservation(self.supervisor, reservation)
            raise
        except Exception as exc:  # noqa: BLE001 - preserve the narrow public capability boundary.
            try:
                if client is not None:
                    await _close_account_bound_client(client)
            finally:
                await _release_attachment_reservation(self.supervisor, reservation)
            raise BuyerTaxonomyRefreshCapabilityError(
                "requested account could not create an account-bound taxonomy reader"
            ) from exc


async def _await_attachment_reservation(
    supervisor: object,
    run_id: str,
    account_registration_id: str,
) -> object:
    """Reserve one idle supervisor binding without exposing worker internals."""

    reserve = getattr(supervisor, "reserve_attachment_identity", None)
    if not callable(reserve):
        raise BuyerAttachmentCapabilitiesUnavailableError(
            "Buyer discovery supervisor cannot reserve an attachment identity"
        )
    try:
        value = reserve(run_id, account_registration_id)
        return await value if inspect.isawaitable(value) else value
    except BuyerAttachmentCapabilitiesUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - preserve the public attachment capability boundary.
        raise BuyerAttachmentCapabilitiesUnavailableError(
            "requested account has no idle Buyer worker with a leased VPNTE route"
        ) from exc


async def _await_account_reservation(supervisor: object, account_registration_id: str) -> object:
    """Reserve an active account identity without exposing fleet internals."""

    reserve = getattr(supervisor, "reserve_account_identity", None)
    if not callable(reserve):
        raise BuyerConversationSyncCapabilityError(
            "Buyer discovery supervisor cannot reserve a conversation identity"
        )
    try:
        value = reserve(account_registration_id)
        return await value if inspect.isawaitable(value) else value
    except BuyerConversationSyncCapabilityError:
        raise
    except Exception as exc:  # noqa: BLE001 - preserve the public conversation capability boundary.
        raise BuyerConversationSyncCapabilityError(
            "requested account has no idle Buyer worker with a leased VPNTE route"
        ) from exc


async def _await_taxonomy_reservation(
    supervisor: object,
    run_id: str,
    account_registration_id: str,
) -> object:
    """Reserve the requested run/account identity for one taxonomy read pass."""

    reserve = getattr(supervisor, "reserve_attachment_identity", None)
    if not callable(reserve):
        raise BuyerTaxonomyRefreshCapabilityError(
            "Buyer discovery supervisor cannot reserve a taxonomy identity"
        )
    try:
        value = reserve(run_id, account_registration_id)
        return await value if inspect.isawaitable(value) else value
    except BuyerTaxonomyRefreshCapabilityError:
        raise
    except Exception as exc:  # noqa: BLE001 - preserve the public taxonomy capability boundary.
        raise BuyerTaxonomyRefreshCapabilityError(
            "requested account has no idle Buyer worker with a leased VPNTE route"
        ) from exc


def _attachment_reservation_identity(reservation: object) -> BuyerDiscoveryIdentity:
    identity = getattr(reservation, "identity", None)
    if not isinstance(identity, BuyerDiscoveryIdentity):
        raise BuyerAttachmentCapabilitiesUnavailableError(
            "Buyer discovery supervisor returned an invalid attachment identity reservation"
        )
    return identity


def _conversation_reservation_identity(reservation: object) -> BuyerDiscoveryIdentity:
    identity = getattr(reservation, "identity", None)
    if not isinstance(identity, BuyerDiscoveryIdentity):
        raise BuyerConversationSyncCapabilityError(
            "Buyer discovery supervisor returned an invalid conversation identity reservation"
        )
    return identity


def _taxonomy_reservation_identity(reservation: object) -> BuyerDiscoveryIdentity:
    identity = getattr(reservation, "identity", None)
    if not isinstance(identity, BuyerDiscoveryIdentity):
        raise BuyerTaxonomyRefreshCapabilityError(
            "Buyer discovery supervisor returned an invalid taxonomy identity reservation"
        )
    return identity


async def _release_attachment_reservation(supervisor: object, reservation: object) -> None:
    release = getattr(supervisor, "release_attachment_identity", None)
    if not callable(release):
        return
    value = release(reservation)
    if inspect.isawaitable(value):
        await value


async def _close_account_bound_client(client: object) -> None:
    close = getattr(client, "close", None)
    if not callable(close):
        return
    value = close()
    if inspect.isawaitable(value):
        await value


def _build_default_account_bound_client(
    context: MarketAccountContext,
    identity: BuyerDiscoveryIdentity,
    snapshot: TransportSnapshot,
) -> AccountBoundBuyerKworkClient:
    return AccountBoundBuyerKworkClient(context, identity, snapshot)


def _backing_job_create(
    run: Mapping[str, Any],
    *,
    account_registration_ids: tuple[str, ...],
    fallback_category_id: int,
) -> MarketJobCreate:
    category_scope = run.get("category_scope")
    scope = category_scope if isinstance(category_scope, Mapping) else {}
    category_id = _optional_positive_int(scope.get("category_id")) or _optional_positive_int(run.get("category_id"))
    category_id = category_id or fallback_category_id
    category_name = _optional_text(scope.get("category_name")) or _optional_text(scope.get("name")) or "Buyer Search"
    filters = run.get("filters") if isinstance(run.get("filters"), Mapping) else {}
    target = _optional_positive_int(run.get("target_unique_projects")) or 1
    desired_workers = _optional_positive_int(run.get("requested_workers")) or 1
    return MarketJobCreate(
        scope=MarketScope(
            category_id=category_id,
            category_name=category_name,
            classifier_id=_optional_positive_int(scope.get("classifier_id")),
            classifier_name=_optional_text(scope.get("classifier_name")) or "",
            canonical_alias=_optional_text(scope.get("canonical_alias")),
            filters=dict(filters),
        ),
        profile="buyer_discovery",
        target_unique_cards=min(target, 10_000),
        desired_workers=desired_workers,
        account_registration_ids=account_registration_ids,
        network_policy=NetworkPolicy.VPNTE_ONLY,
        source_policy=SourcePolicy.MOBILE_FIRST_PAGE_ONLY,
        include_ai=False,
        job_kind=JobKind.BUYER_SEARCH,
        workflow_config={
            "buyer_run_id": _required_text(run.get("run_id"), "run.run_id"),
            "runtime": "buyer_discovery",
            "owns_supply_operations": False,
        },
    )


def _validate_account_identity(context: MarketAccountContext, identity: BuyerDiscoveryIdentity) -> None:
    if not isinstance(context, MarketAccountContext):
        raise TypeError("context must be MarketAccountContext")
    if not isinstance(identity, BuyerDiscoveryIdentity):
        raise TypeError("identity must be BuyerDiscoveryIdentity")
    if context.registration_id != identity.account_registration_id:
        raise BuyerAccountClientFactoryError("account context does not match the Buyer discovery identity")
    has_cookies = any(str(name).strip() and str(value).strip() for name, value in context.cookies.items())
    has_credentials = bool(context.email.strip() and context.password)
    if not has_cookies and not has_credentials:
        raise BuyerAccountClientFactoryError("account context has neither authenticated cookies nor explicit credentials")


def _validate_route_snapshot(snapshot: TransportSnapshot | None, identity: BuyerDiscoveryIdentity) -> TransportSnapshot:
    if not isinstance(snapshot, TransportSnapshot):
        raise BuyerTransportUnavailableError(f"Buyer worker {identity.worker_id!r} has no VPNTE route snapshot")
    if snapshot.transport_id != identity.transport_id:
        raise BuyerTransportUnavailableError("VPNTE snapshot does not match the Buyer identity transport")
    if snapshot.kind is not TransportKind.VPNTE:
        raise BuyerTransportUnavailableError("Buyer discovery rejects non-VPNTE transport routes")
    if snapshot.health is not TransportHealth.HEALTHY:
        raise BuyerTransportUnavailableError("Buyer discovery route is not healthy")
    if not _optional_text(snapshot.proxy_url):
        raise BuyerTransportUnavailableError("Buyer discovery route has no healthy VPNTE proxy URL")
    if snapshot.lease_owner != identity.worker_id:
        raise BuyerTransportUnavailableError("VPNTE route is not leased by this Buyer worker")
    if snapshot.egress_ip != identity.egress_ip:
        raise BuyerTransportUnavailableError("VPNTE route egress IP no longer matches the Buyer identity")
    if identity.route_generation is not None and snapshot.generation != identity.route_generation:
        raise BuyerTransportUnavailableError("VPNTE route generation no longer matches the Buyer identity")
    return snapshot


def _web_project_params(params: Mapping[str, Any]) -> dict[str, Any]:
    page = _optional_positive_int(params.get("page")) or 1
    result: dict[str, Any] = {"page": page}
    category_id = _optional_positive_int(params.get("category_id"))
    if category_id is not None:
        result["c"] = category_id
    query = _optional_text(params.get("query"))
    if query:
        result["keyword"] = query
    price_from = _optional_nonnegative_int(params.get("price_from"))
    if price_from is not None:
        result["price-from"] = price_from
    price_to = _optional_nonnegative_int(params.get("price_to"))
    if price_to is not None:
        result["price-to"] = price_to
    hiring_from = _optional_nonnegative_int(params.get("hiring_from"))
    if hiring_from is not None:
        result["hiring-from"] = hiring_from
    kworks_filter = _web_projects_filter_id(
        _optional_nonnegative_int(params.get("kworks_filter_from")),
        _optional_nonnegative_int(params.get("kworks_filter_to")),
    )
    if kworks_filter is not None:
        result["kworks-filters"] = kworks_filter
    prices_filters = _optional_text(params.get("prices_filters"))
    if prices_filters:
        result["prices-filters"] = prices_filters
    for key in ("sort", "status"):
        value = _optional_text(params.get(key))
        if value:
            result[key] = value
    return result


def _web_projects_filter_id(kworks_filter_from: int | None, kworks_filter_to: int | None) -> str | None:
    if kworks_filter_from in (None, 0) and kworks_filter_to is not None and kworks_filter_to <= 5:
        return "0"
    if kworks_filter_from == 5 and kworks_filter_to == 10:
        return "1"
    if kworks_filter_from == 10 and kworks_filter_to == 15:
        return "2"
    if kworks_filter_from == 15 and kworks_filter_to == 20:
        return "3"
    if kworks_filter_from is not None and kworks_filter_from >= 20 and kworks_filter_to is None:
        return "4"
    return None


def _web_state_items(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    wants: Any = state.get("wants")
    if isinstance(wants, Mapping):
        wants = wants.get("data") or wants.get("items") or wants.get("wants")
    if not isinstance(wants, list):
        pagination = state.get("pagination")
        wants = pagination.get("data") if isinstance(pagination, Mapping) else None
    return [dict(item) for item in wants if isinstance(item, Mapping)] if isinstance(wants, list) else []


def _extract_web_chat_list(html: str) -> list[dict[str, Any]]:
    """Return the embedded inbox list without invoking the shared Kwork service."""

    marker = "window.chatList="
    index = html.find(marker)
    if index < 0:
        return []
    raw = html[index + len(marker) :].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _normalize_web_inbox_dialog(item: Mapping[str, Any]) -> dict[str, Any] | None:
    author = item.get("author") if isinstance(item.get("author"), Mapping) else {}
    last_message = item.get("lastMessage") if isinstance(item.get("lastMessage"), Mapping) else {}
    username = _first_mapping_text(item, "username") or _first_mapping_text(author, "username")
    remote_user_id = (
        _first_mapping_text(item, "user_id", "userId")
        or _first_mapping_text(author, "USERID", "user_id", "id")
    )
    remote_dialog_id = _first_mapping_text(item, "dialog_id", "dialogId") or remote_user_id or username
    if remote_dialog_id is None:
        return None
    member_id = _first_mapping_text(item, "member_id", "memberId")
    last_message_at = (
        _first_mapping_text(last_message, "time", "created_at", "sent_at")
        or _first_mapping_text(item, "time", "created_at", "updated_at")
    )
    result: dict[str, Any] = {
        "remote_dialog_id": remote_dialog_id,
        "dialog_id": remote_dialog_id,
        "username": username,
        "user_id": remote_user_id,
        "remote_title": _first_mapping_text(item, "project_name", "title") or username,
        "last_message_at": last_message_at,
        "unread_count": item.get("unread_count") or item.get("unread") or 0,
    }
    if member_id is not None:
        result["member_id"] = member_id
    return result


def _inbox_dialog_username(dialog: Mapping[str, Any]) -> str:
    username = _first_mapping_text(dialog, "username", "buyer_username")
    if username is None:
        raise BuyerAccountBoundKworkClientError("Kwork inbox dialog does not expose a username for history retrieval")
    return _required_text(username.strip("/"), "dialog.username")


def _inbox_message_items(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    for key in ("messages", "items", "response"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            for nested_key in ("messages", "items", "data"):
                nested = value.get(nested_key)
                if isinstance(nested, Sequence) and not isinstance(nested, (str, bytes, bytearray)):
                    return [dict(item) for item in nested if isinstance(item, Mapping)]
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


def _inbox_page_count(payload: Mapping[str, Any], *, current_page: int) -> int:
    paging = payload.get("paging") if isinstance(payload.get("paging"), Mapping) else {}
    pages = _optional_positive_int(paging.get("pages") or paging.get("total_pages") or payload.get("pages"))
    return max(current_page, pages or current_page)


def _normalize_inbox_message(item: Mapping[str, Any], *, member_id: str | None) -> dict[str, Any]:
    normalized = dict(item)
    remote_message_id = _first_mapping_text(item, "remote_message_id", "message_id", "inbox_message_id", "MID", "id")
    if remote_message_id is not None:
        normalized["remote_message_id"] = remote_message_id
    sender_id = _first_mapping_text(item, "remote_sender_id", "sender_id", "from_id", "MSGFROM")
    if sender_id is not None:
        normalized["remote_sender_id"] = sender_id
    created_at = _first_mapping_text(item, "remote_created_at", "created_at", "sent_at", "date", "time")
    if created_at is not None:
        normalized["remote_created_at"] = created_at
    if member_id is not None and sender_id is not None and sender_id == member_id:
        normalized["direction"] = "outgoing"
    return normalized


def _inbox_watermark(dialogs: Sequence[Mapping[str, Any]]) -> str | None:
    for dialog in dialogs:
        watermark = _optional_text(dialog.get("last_message_at"))
        if watermark is not None:
            return watermark
    return None


def _first_mapping_text(payload: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = _optional_text(payload.get(key))
        if value is not None:
            return value
    return None


def _as_account_client_error(exc: Exception, prefix: str) -> BuyerAccountBoundKworkClientError:
    status_code = getattr(exc, "status_code", None)
    if not isinstance(status_code, int):
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    headers = getattr(getattr(exc, "response", None), "headers", None)
    retry_after = _retry_after_seconds(headers.get("retry-after")) if isinstance(headers, Mapping) else None
    detail = str(exc).strip() or type(exc).__name__
    return BuyerAccountBoundKworkClientError(f"{prefix}: {detail}", status_code=status_code, retry_after_seconds=retry_after)


def _retry_after_seconds(value: object) -> float | None:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _validate_kwork_asset_url(value: object) -> str:
    """Limit attachment reads to HTTPS Kwork/CDN hosts captured by discovery."""

    url = _required_text(value, "attachment_url")
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    is_kwork_host = hostname == "kwork.ru" or hostname.endswith(".kwork.ru")
    if parsed.scheme.casefold() != "https" or not is_kwork_host:
        raise BuyerAccountBoundKworkClientError("attachment URL must use a Kwork HTTPS host")
    return url


def _bounded_download_bytes(value: object) -> int:
    if value is None:
        return 10 * 1024 * 1024
    parsed = _positive_int(value, "max_bytes")
    return min(parsed, 100 * 1024 * 1024)


def _bounded_read_timeout(value: object) -> float:
    if value is None:
        return 20.0
    if isinstance(value, bool):
        raise ValueError("timeout_seconds must be a positive number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout_seconds must be a positive number") from exc
    if not 0 < parsed <= 120:
        raise ValueError("timeout_seconds must be between 0 and 120")
    return parsed


def _filename_from_content_disposition(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    for segment in value.split(";"):
        name, separator, raw = segment.strip().partition("=")
        if not separator or name.casefold() not in {"filename", "filename*"}:
            continue
        candidate = raw.strip().strip('"')
        if name.casefold() == "filename*" and "''" in candidate:
            candidate = candidate.split("''", 1)[1]
        candidate = unquote(candidate).replace("\\", "/").rsplit("/", 1)[-1].strip()
        if candidate:
            return candidate[:255]
    return None


def _normalize_registration_ids(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError("account_registration_ids must be a sequence of strings")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        normalized = _required_text(item, "account_registration_id")
        if normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return tuple(result)


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} cannot be blank")
    return normalized


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if result <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return result


def _optional_positive_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


__all__ = [
    "AccountBoundBuyerAttachmentCapabilities",
    "AccountBoundBuyerAttachmentCapabilitiesFactory",
    "AccountBoundBuyerConversationCapabilitiesFactory",
    "AccountBoundBuyerTaxonomyCapabilitiesFactory",
    "AccountBoundBuyerKworkClient",
    "AccountBoundBuyerKworkClientFactory",
    "BUYER_BACKING_JOB_PREFIX",
    "BUYER_BACKING_SCOPE_CATEGORY_ID",
    "BuyerAccountBoundKworkClientError",
    "BuyerAccountClientFactoryError",
    "BuyerAttachmentCapabilitiesUnavailableError",
    "BuyerBackingJobError",
    "BuyerRuntimeCompositionError",
    "BuyerRunMarketJobResolver",
    "BuyerTransportUnavailableError",
]
