from __future__ import annotations

import sqlite3

from src.action.proposal_db import ProposalDB


def test_account_bound_conversations_do_not_mix_identical_project_ids(tmp_path):
    db = ProposalDB(db_path=str(tmp_path / "proposals.sqlite3"))
    first = db.get_or_create_conversation(
        "project-1",
        "kwork",
        account_registration_id="account-a",
        remote_dialog_id="dialog-1",
        project_title="First account",
    )
    second = db.get_or_create_conversation(
        "project-1",
        "kwork",
        account_registration_id="account-b",
        remote_dialog_id="dialog-1",
        project_title="Second account",
    )
    assert first != second

    assert db.add_conversation_message(first, sender="customer", message_text="A")
    assert db.add_conversation_message(second, sender="customer", message_text="B")

    conversation_a = db.get_conversation("project-1", "kwork", account_registration_id="account-a")
    conversation_b = db.get_conversation_by_remote_dialog("kwork", "account-b", "dialog-1")
    assert conversation_a is not None and conversation_b is not None
    assert [message["message_text"] for message in conversation_a["messages"]] == ["A"]
    assert [message["message_text"] for message in conversation_b["messages"]] == ["B"]


def test_legacy_conversation_calls_remain_in_the_empty_account_scope(tmp_path):
    db = ProposalDB(db_path=str(tmp_path / "proposals.sqlite3"))
    conversation_id = db.get_or_create_conversation("legacy-project", "kwork")

    conversation = db.get_conversation("legacy-project", "kwork")
    assert conversation is not None
    assert conversation["conversation_id"] == conversation_id
    assert conversation["account_registration_id"] == ""


def test_account_scope_migrates_a_legacy_conversation_table_before_creating_indexes(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE conversations (
                conversation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER,
                project_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                project_title TEXT,
                status TEXT DEFAULT 'new',
                last_message_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute("CREATE UNIQUE INDEX idx_conv_project_platform ON conversations(project_id, platform)")

    db = ProposalDB(db_path=str(db_path))
    conversation_id = db.get_or_create_conversation("legacy-project", "kwork")
    conversation = db.get_conversation("legacy-project", "kwork")
    assert conversation is not None
    assert conversation["conversation_id"] == conversation_id
    assert conversation["account_registration_id"] == ""
