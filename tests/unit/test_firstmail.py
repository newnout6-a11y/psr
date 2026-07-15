from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.utils.firstmail import FirstmailClient


def test_firstmail_extracts_kwork_link_from_html_message():
    payload = {
        "data": [
            {
                "from": "noreply@kwork.ru",
                "subject": "Подтверждение регистрации",
                "html": '<a href="https://kwork.ru/user/activate?token=abc">Activate</a>',
            }
        ]
    }

    assert FirstmailClient.find_kwork_link(payload) == "https://kwork.ru/user/activate?token=abc"


def test_firstmail_normalizes_single_message_envelope():
    payload = {
        "subject": "Activation",
        "body": "https://www.kwork.ru/account/activate?token=xyz",
    }

    assert FirstmailClient.find_kwork_link(payload) == "https://www.kwork.ru/account/activate?token=xyz"


def test_firstmail_ignores_stale_message_when_start_time_is_given():
    payload = {
        "data": [
            {
                "date": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
                "body": "https://kwork.ru/account/activate?token=old",
            }
        ]
    }

    assert FirstmailClient.find_kwork_link(payload, after=datetime.now(UTC)) is None
