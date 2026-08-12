from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import src.platforms.kwork_buyer.runtime_composition as runtime_composition
from src.platforms.kwork_buyer.runtime_composition import (
    AccountBoundBuyerAttachmentCapabilitiesFactory,
    AccountBoundBuyerKworkClient,
    BuyerAccountBoundKworkClientError,
)
from src.platforms.kwork_buyer.worker import BuyerDiscoveryIdentity
from src.platforms.kwork_supply.identity_pool import MarketAccountContext


def _context() -> MarketAccountContext:
    return MarketAccountContext(
        registration_id="account-1",
        username="buyer",
        email="buyer@example.test",
        password="secret",
        cookies={"PHPSESSID": "session"},
        headers={"X-Persona": "buyer-test"},
        persona_id="persona-1",
        signup_ip="198.51.100.10",
        preferred_slot=1,
    )


class _Supervisor:
    def __init__(self) -> None:
        self.reserve_calls: list[tuple[str, str]] = []
        self.release_calls: list[str] = []
        self._reservation_lock = asyncio.Lock()
        self.identity = BuyerDiscoveryIdentity(
            worker_id="buyer-worker-1",
            account_registration_id="account-1",
            transport_id="vpnte-slot-1",
            egress_ip="198.51.100.10",
        )

    async def reserve_attachment_identity(self, run_id: str, account_registration_id: str) -> object:
        self.reserve_calls.append((run_id, account_registration_id))
        await self._reservation_lock.acquire()
        return SimpleNamespace(
            run_id=run_id,
            worker_id=self.identity.worker_id,
            identity=self.identity,
            token=f"reservation-{len(self.reserve_calls)}",
        )

    async def release_attachment_identity(self, reservation: object) -> None:
        self.release_calls.append(str(reservation.token))  # type: ignore[attr-defined]
        if self._reservation_lock.locked():
            self._reservation_lock.release()


class _IdentityPool:
    def __init__(self) -> None:
        self.context_calls: list[str] = []

    async def context_for_worker(self, worker_id: str) -> MarketAccountContext | None:
        self.context_calls.append(worker_id)
        return _context()

    async def release(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("attachment capability must not release or rotate the leased route")


class _AttachmentClient:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _AttachmentClientFactory:
    def __init__(self, clients: list[_AttachmentClient]) -> None:
        self.clients = clients
        self.calls: list[tuple[MarketAccountContext, object]] = []

    async def __call__(self, context: MarketAccountContext, identity: object) -> _AttachmentClient:
        self.calls.append((context, identity))
        return self.clients.pop(0)


@pytest.mark.asyncio
async def test_attachment_capability_borrows_and_releases_a_supervisor_owned_identity_reservation() -> None:
    supervisor = _Supervisor()
    identity_pool = _IdentityPool()
    first_client = _AttachmentClient()
    second_client = _AttachmentClient()
    client_factory = _AttachmentClientFactory([first_client, second_client])
    factory = AccountBoundBuyerAttachmentCapabilitiesFactory(
        supervisor,
        identity_pool,  # type: ignore[arg-type]
        client_factory,  # type: ignore[arg-type]
    )

    first = await factory("run-1", "project-1", "account-1")
    second_task = asyncio.create_task(factory("run-1", "project-2", "account-1"))
    await asyncio.sleep(0)

    assert not second_task.done()
    assert first.provenance.as_dict() == {
        "worker_id": "buyer-worker-1",
        "account_registration_id": "account-1",
        "transport_id": "vpnte-slot-1",
        "egress_ip": "198.51.100.10",
        "source": "attachment_enrichment",
    }

    await first.close()
    await first.close()
    second = await second_task
    await second.close()

    assert first_client.close_calls == 1
    assert second_client.close_calls == 1
    assert supervisor.reserve_calls == [("run-1", "account-1"), ("run-1", "account-1")]
    assert supervisor.release_calls == ["reservation-1", "reservation-2"]
    assert identity_pool.context_calls == ["buyer-worker-1", "buyer-worker-1"]
    assert [identity.account_registration_id for _, identity in client_factory.calls] == ["account-1", "account-1"]


class _FakeResponse:
    def __init__(
        self,
        *,
        url: str,
        headers: dict[str, str] | None = None,
        chunks: tuple[bytes, ...] = (),
        status_code: int = 200,
    ) -> None:
        self.url = url
        self.headers = headers or {}
        self.chunks = chunks
        self.status_code = status_code
        self.iterated = False

    async def aiter_bytes(self):
        self.iterated = True
        for chunk in self.chunks:
            yield chunk


class _FakeStream:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    async def __aenter__(self) -> _FakeResponse:
        return self.response

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakeAsyncClient:
    response: _FakeResponse
    constructions: list[dict[str, Any]] = []
    streams: list[tuple[str, str]] = []

    def __init__(self, **kwargs: Any) -> None:
        type(self).constructions.append(kwargs)

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def stream(self, method: str, url: str) -> _FakeStream:
        type(self).streams.append((method, url))
        return _FakeStream(type(self).response)


def _attachment_client() -> AccountBoundBuyerKworkClient:
    client = object.__new__(AccountBoundBuyerKworkClient)
    client.context = _context()
    client.proxy_url = "http://127.0.0.1:19001"
    return client


@pytest.mark.asyncio
async def test_attachment_download_uses_the_leased_proxy_and_bounds_the_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncClient.constructions = []
    _FakeAsyncClient.streams = []
    _FakeAsyncClient.response = _FakeResponse(
        url="https://files.kwork.ru/uploads/brief.txt",
        headers={
            "content-type": "text/plain",
            "content-disposition": "attachment; filename*=UTF-8''brief%20one.txt",
        },
        chunks=(b"scope: ", b"telegram"),
    )
    monkeypatch.setattr(runtime_composition.httpx, "AsyncClient", _FakeAsyncClient)

    result = await _attachment_client().download_attachment(
        "https://files.kwork.ru/uploads/brief.txt?signature=opaque",
        max_bytes=32,
        timeout_seconds=9,
    )

    assert result == {
        "content": b"scope: telegram",
        "filename": "brief one.txt",
        "content_type": "text/plain",
        "resolved_download_url": "https://files.kwork.ru/uploads/brief.txt",
    }
    assert _FakeAsyncClient.streams == [("GET", "https://files.kwork.ru/uploads/brief.txt?signature=opaque")]
    assert _FakeAsyncClient.constructions == [
        {
            "headers": {
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                "User-Agent": "PSR-BuyerAttachment/1.0",
                "X-Persona": "buyer-test",
            },
            "cookies": {"PHPSESSID": "session"},
            "timeout": 9.0,
            "follow_redirects": False,
            "proxy": "http://127.0.0.1:19001",
            "trust_env": False,
        }
    ]


@pytest.mark.asyncio
async def test_attachment_download_rejects_non_kwork_hosts_before_creating_a_client(monkeypatch: pytest.MonkeyPatch) -> None:
    class _UnexpectedAsyncClient:
        def __init__(self, **_kwargs: Any) -> None:
            raise AssertionError("unapproved attachment hosts must not open a route")

    monkeypatch.setattr(runtime_composition.httpx, "AsyncClient", _UnexpectedAsyncClient)

    with pytest.raises(BuyerAccountBoundKworkClientError, match="Kwork HTTPS host"):
        await _attachment_client().download_attachment("https://notkwork.ru/uploads/brief.txt")


@pytest.mark.asyncio
async def test_attachment_download_rejects_external_redirect_before_requesting_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAsyncClient.constructions = []
    _FakeAsyncClient.streams = []
    _FakeAsyncClient.response = _FakeResponse(
        url="https://files.kwork.ru/uploads/brief.txt",
        headers={"location": "https://attacker.example/brief.txt"},
        status_code=302,
    )
    monkeypatch.setattr(runtime_composition.httpx, "AsyncClient", _FakeAsyncClient)

    with pytest.raises(BuyerAccountBoundKworkClientError, match="Kwork HTTPS host"):
        await _attachment_client().download_attachment("https://files.kwork.ru/uploads/brief.txt")

    assert _FakeAsyncClient.streams == [("GET", "https://files.kwork.ru/uploads/brief.txt")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "chunks"),
    [
        ({"content-length": "7"}, ()),
        ({}, (b"123", b"456", b"7")),
    ],
)
async def test_attachment_download_rejects_declared_and_streamed_oversize_content(
    monkeypatch: pytest.MonkeyPatch,
    headers: dict[str, str],
    chunks: tuple[bytes, ...],
) -> None:
    _FakeAsyncClient.constructions = []
    _FakeAsyncClient.streams = []
    _FakeAsyncClient.response = _FakeResponse(
        url="https://files.kwork.ru/uploads/brief.txt",
        headers=headers,
        chunks=chunks,
    )
    monkeypatch.setattr(runtime_composition.httpx, "AsyncClient", _FakeAsyncClient)

    with pytest.raises(BuyerAccountBoundKworkClientError, match="exceeds 6 byte limit"):
        await _attachment_client().download_attachment("https://files.kwork.ru/uploads/brief.txt", max_bytes=6)

    if headers:
        assert not _FakeAsyncClient.response.iterated
