from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from src.utils.catchmail import CatchmailClient


def test_catchmail_generates_kwork_compatible_address():
    address = CatchmailClient.generate_address()

    local, domain = address.split("@")
    assert domain == "catchmail.io"
    assert local.startswith("psr-")
    assert 4 <= len(local) <= 20


def test_catchmail_extracts_kwork_link_from_message_body():
    message = {
        "body": {
            "html": '<p><a href="https://kwork.ru/user/activate?token=abc">Activate</a></p>',
        }
    }

    assert CatchmailClient.extract_kwork_link(message) == "https://kwork.ru/user/activate?token=abc"


def test_catchmail_ignores_stale_message_when_timestamp_is_present():
    message = {"receivedAt": (datetime.now(UTC) - timedelta(minutes=5)).isoformat()}

    assert CatchmailClient._message_is_after(message, datetime.now(UTC)) is False


@pytest.mark.asyncio
async def test_catchmail_reads_message_detail_before_returning_link(monkeypatch):
    client = CatchmailClient(email="psr-test@catchmail.io")
    mailbox = AsyncMock(return_value={"messages": [{"id": "message-1", "receivedAt": datetime.now(UTC).isoformat()}]})
    message = AsyncMock(
        return_value={
            "id": "message-1",
            "body": {"text": "Use https://www.kwork.ru/account/activate?token=xyz to activate"},
        }
    )
    monkeypatch.setattr(CatchmailClient, "mailbox", mailbox)
    monkeypatch.setattr(CatchmailClient, "message", message)

    link = await client.wait_for_kwork_link(timeout=1, poll_interval=1.1, after=datetime.now(UTC) - timedelta(seconds=1))

    assert link == "https://www.kwork.ru/account/activate?token=xyz"
    mailbox.assert_awaited_once_with()
    message.assert_awaited_once_with("message-1")


@pytest.mark.asyncio
async def test_catchmail_waits_before_the_first_mailbox_poll(monkeypatch):
    client = CatchmailClient(email="psr-delay@catchmail.io")
    mailbox = AsyncMock(return_value={"messages": [{"id": "message-1"}]})
    message = AsyncMock(return_value={"body": {"text": "https://kwork.ru/confirmemail?c=xyz"}})
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(CatchmailClient, "mailbox", mailbox)
    monkeypatch.setattr(CatchmailClient, "message", message)
    monkeypatch.setattr("src.utils.catchmail.asyncio.sleep", fake_sleep)

    link = await client.wait_for_kwork_link(timeout=30, poll_interval=5, initial_delay=7)

    assert link == "https://kwork.ru/confirmemail?c=xyz"
    assert sleeps == [7]
