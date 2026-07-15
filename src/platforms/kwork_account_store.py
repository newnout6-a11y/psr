"""Durable clear-text storage for locally managed Kwork registrations."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

from cryptography.fernet import Fernet, InvalidToken

from src.paths import KWORK_ACCOUNTS_DB_FILE, KWORK_ACCOUNTS_KEY_FILE, ensure_parent


class KworkAccountStoreError(RuntimeError):
    """Raised when a stored registration cannot be read or migrated."""


@dataclass(frozen=True, slots=True)
class StoredKworkAccount:
    """One registration record and the credentials needed to reuse its session."""

    registration_id: str
    email: str
    username: str
    user_type: int
    mail_provider: str
    status: str
    created_at: str
    registration_started_at: str
    activated_at: str | None
    signup_ip: str | None
    activation_ip: str | None
    registration_slot: int | None
    registration_proxy_url: str | None
    cookie_count: int
    last_error: str | None
    market_enabled: bool
    persona: dict[str, Any]
    password: str
    session: dict[str, Any]

    def public_data(self) -> dict[str, Any]:
        return {
            "registration_id": self.registration_id,
            "email": self.email,
            "username": self.username,
            "user_type": self.user_type,
            "mail_provider": self.mail_provider,
            "status": self.status,
            "created_at": self.created_at,
            "registration_started_at": self.registration_started_at,
            "activated_at": self.activated_at,
            "signup_ip": self.signup_ip,
            "activation_ip": self.activation_ip,
            "registration_slot": self.registration_slot,
            "registration_proxy_url": self.registration_proxy_url,
            "session_cookie_count": self.cookie_count,
            "last_error": self.last_error,
            "market_enabled": self.market_enabled,
            "persona_id": self.persona.get("persona_id"),
        }


class KworkAccountStore:
    """SQLite account store with plaintext credentials and full session JSON."""

    def __init__(
        self,
        *,
        db_path: Path = KWORK_ACCOUNTS_DB_FILE,
        key_path: Path = KWORK_ACCOUNTS_KEY_FILE,
    ) -> None:
        self.db_path = Path(db_path)
        self.key_path = Path(key_path)
        self._init_db()

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _connect(self) -> sqlite3.Connection:
        ensure_parent(self.db_path)
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _column_names(connection: sqlite3.Connection) -> set[str]:
        rows = connection.execute("PRAGMA table_info(kwork_registration_accounts)").fetchall()
        return {str(row["name"]) for row in rows}

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, name: str, ddl: str) -> None:
        if name not in KworkAccountStore._column_names(connection):
            connection.execute(f"ALTER TABLE kwork_registration_accounts ADD COLUMN {name} {ddl}")

    @staticmethod
    def _default_persona(registration_id: str) -> dict[str, Any]:
        """Create a stable desktop request persona for one locally managed account."""

        digest = sha256(registration_id.encode("utf-8")).hexdigest()
        seed = int(digest[:12], 16)
        locales = ("ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7", "en-US,en;q=0.9,ru;q=0.7")
        timezones = ("Europe/Moscow", "Europe/Riga", "Europe/Warsaw", "Europe/Vilnius")
        viewports = ((1366, 768), (1440, 900), (1536, 864), (1920, 1080))
        chrome_major = 124 + (seed % 8)
        chrome_build = 6367 + ((seed >> 8) % 180)
        chrome_patch = 40 + ((seed >> 16) % 900)
        width, height = viewports[(seed >> 24) % len(viewports)]
        return {
            "persona_id": f"machine-{digest[:16]}",
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{chrome_major}.0.{chrome_build}.{chrome_patch} Safari/537.36"
            ),
            "accept_language": locales[(seed >> 32) % len(locales)],
            "timezone": timezones[(seed >> 36) % len(timezones)],
            "viewport": {"width": width, "height": height},
        }

    def _legacy_fernet(self) -> Fernet:
        raw_key = os.getenv("KWORK_ACCOUNT_STORE_KEY", "").strip()
        if not raw_key and self.key_path.exists():
            raw_key = self.key_path.read_bytes().strip()
        if isinstance(raw_key, str):
            raw_key = raw_key.encode("ascii")
        try:
            return Fernet(raw_key)
        except (TypeError, ValueError) as exc:
            raise KworkAccountStoreError("Legacy encrypted Kwork account records cannot be migrated without their key") from exc

    def _migrate_legacy_encrypted_rows(self, connection: sqlite3.Connection) -> None:
        columns = self._column_names(connection)
        if not {"password_ciphertext", "session_ciphertext"}.issubset(columns):
            return
        rows = connection.execute(
            """
            SELECT registration_id, password_ciphertext, session_ciphertext
            FROM kwork_registration_accounts
            WHERE password IS NULL OR session_json IS NULL
            """
        ).fetchall()
        if not rows:
            return
        cipher = self._legacy_fernet()
        for row in rows:
            try:
                password = cipher.decrypt(str(row["password_ciphertext"]).encode("ascii")).decode("utf-8")
                session_json = cipher.decrypt(str(row["session_ciphertext"]).encode("ascii")).decode("utf-8")
                session = json.loads(session_json)
            except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise KworkAccountStoreError("Legacy encrypted Kwork account record cannot be migrated") from exc
            if not isinstance(session, dict):
                raise KworkAccountStoreError("Legacy Kwork account session has an invalid format")
            connection.execute(
                """
                UPDATE kwork_registration_accounts
                SET password = ?, session_json = ?, password_ciphertext = '', session_ciphertext = ''
                WHERE registration_id = ?
                """,
                (password, json.dumps(session, ensure_ascii=False, separators=(",", ":"), sort_keys=True), row["registration_id"]),
            )

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS kwork_registration_accounts (
                    registration_id TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    username TEXT NOT NULL,
                    user_type INTEGER NOT NULL,
                    mail_provider TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    registration_started_at TEXT NOT NULL,
                    activated_at TEXT,
                    signup_ip TEXT,
                    activation_ip TEXT,
                    registration_slot INTEGER,
                    registration_proxy_url TEXT,
                    cookie_count INTEGER NOT NULL DEFAULT 0,
                    password_ciphertext TEXT NOT NULL DEFAULT '',
                    session_ciphertext TEXT NOT NULL DEFAULT '',
                    password TEXT,
                    session_json TEXT,
                    last_error TEXT,
                    market_enabled INTEGER NOT NULL DEFAULT 1,
                    persona_json TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(connection, "password", "TEXT")
            self._ensure_column(connection, "session_json", "TEXT")
            self._ensure_column(connection, "registration_slot", "INTEGER")
            self._ensure_column(connection, "registration_proxy_url", "TEXT")
            self._ensure_column(connection, "market_enabled", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(connection, "persona_json", "TEXT")
            self._migrate_legacy_encrypted_rows(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_kwork_registration_accounts_email
                ON kwork_registration_accounts(email)
                """
            )

    def save(
        self,
        *,
        registration_id: str,
        email: str,
        username: str,
        user_type: int,
        mail_provider: str,
        status: str,
        registration_started_at: str,
        password: str,
        session: Mapping[str, Any],
        signup_ip: str | None = None,
        activation_ip: str | None = None,
        registration_slot: int | None = None,
        registration_proxy_url: str | None = None,
        persona: Mapping[str, Any] | None = None,
        market_enabled: bool | None = None,
        activated_at: str | None = None,
        last_error: str | None = None,
    ) -> StoredKworkAccount:
        """Insert or update one registration with plaintext password and session data."""

        registration_id = str(registration_id).strip()
        if not registration_id:
            raise KworkAccountStoreError("registration_id is required")
        password = str(password)
        if not password:
            raise KworkAccountStoreError("Kwork password cannot be empty")
        plain_session = dict(session)
        now = self._timestamp()
        normalized_slot = int(registration_slot) if registration_slot is not None else None
        if normalized_slot is not None and normalized_slot < 1:
            raise KworkAccountStoreError("registration_slot must be positive")
        default_persona = self._default_persona(registration_id)
        normalized_persona = dict(persona) if isinstance(persona, Mapping) else default_persona
        if not str(normalized_persona.get("persona_id") or "").strip():
            normalized_persona["persona_id"] = default_persona["persona_id"]
        values = {
            "registration_id": registration_id,
            "email": str(email).strip().lower(),
            "username": str(username).strip(),
            "user_type": int(user_type),
            "mail_provider": str(mail_provider).strip().lower(),
            "status": str(status).strip(),
            "created_at": now,
            "registration_started_at": str(registration_started_at).strip(),
            "activated_at": activated_at,
            "signup_ip": signup_ip,
            "activation_ip": activation_ip,
            "registration_slot": normalized_slot,
            "registration_proxy_url": str(registration_proxy_url or plain_session.get("proxy_url") or "").strip() or None,
            "cookie_count": len(plain_session.get("cookies", [])) if isinstance(plain_session.get("cookies"), list) else 0,
            "password": password,
            "session_json": json.dumps(plain_session, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            "last_error": last_error,
            "market_enabled": 1 if market_enabled is not False else 0,
            "persona_json": json.dumps(normalized_persona, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            "updated_at": now,
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO kwork_registration_accounts (
                    registration_id, email, username, user_type, mail_provider, status,
                    created_at, registration_started_at, activated_at, signup_ip,
                    activation_ip, registration_slot, registration_proxy_url, cookie_count,
                    password_ciphertext, session_ciphertext, password, session_json, last_error,
                    market_enabled, persona_json, updated_at
                ) VALUES (
                    :registration_id, :email, :username, :user_type, :mail_provider, :status,
                    :created_at, :registration_started_at, :activated_at, :signup_ip,
                    :activation_ip, :registration_slot, :registration_proxy_url, :cookie_count,
                    '', '', :password, :session_json, :last_error, :market_enabled,
                    :persona_json, :updated_at
                )
                ON CONFLICT(registration_id) DO UPDATE SET
                    email = excluded.email,
                    username = excluded.username,
                    user_type = excluded.user_type,
                    mail_provider = excluded.mail_provider,
                    status = excluded.status,
                    registration_started_at = excluded.registration_started_at,
                    activated_at = COALESCE(excluded.activated_at, kwork_registration_accounts.activated_at),
                    signup_ip = COALESCE(excluded.signup_ip, kwork_registration_accounts.signup_ip),
                    activation_ip = COALESCE(excluded.activation_ip, kwork_registration_accounts.activation_ip),
                    registration_slot = COALESCE(excluded.registration_slot, kwork_registration_accounts.registration_slot),
                    registration_proxy_url = COALESCE(excluded.registration_proxy_url, kwork_registration_accounts.registration_proxy_url),
                    cookie_count = excluded.cookie_count,
                    password_ciphertext = '',
                    session_ciphertext = '',
                    password = excluded.password,
                    session_json = excluded.session_json,
                    last_error = excluded.last_error,
                    market_enabled = CASE
                        WHEN excluded.market_enabled = 0 THEN 0
                        ELSE kwork_registration_accounts.market_enabled
                    END,
                    persona_json = COALESCE(NULLIF(excluded.persona_json, ''), kwork_registration_accounts.persona_json),
                    updated_at = excluded.updated_at
                """,
                values,
            )
        saved = self.get(registration_id)
        if saved is None:
            raise KworkAccountStoreError("Saved Kwork registration record cannot be read back")
        return saved

    def get(self, registration_id: str) -> StoredKworkAccount | None:
        """Read a record from the local clear-text account store."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM kwork_registration_accounts WHERE registration_id = ?",
                (str(registration_id).strip(),),
            ).fetchone()
        if row is None:
            return None
        return self._record_from_row(row)

    def list(self) -> list[StoredKworkAccount]:
        """Return locally stored registrations, newest first, without exposing raw SQL to callers."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM kwork_registration_accounts
                ORDER BY created_at DESC, registration_id DESC
                """
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def delete(self, registration_id: str) -> bool:
        """Delete one local record only; this never contacts Kwork."""

        clean_registration_id = str(registration_id).strip()
        if not clean_registration_id:
            return False
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM kwork_registration_accounts WHERE registration_id = ?",
                (clean_registration_id,),
            )
        return cursor.rowcount > 0

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> StoredKworkAccount:
        """Decode one SQLite row into the public store record shape."""

        try:
            session = json.loads(str(row["session_json"] or "{}"))
        except json.JSONDecodeError as exc:
            raise KworkAccountStoreError("Saved Kwork registration session has an invalid JSON format") from exc
        if not isinstance(session, dict):
            raise KworkAccountStoreError("Saved Kwork registration session has an invalid format")
        password = str(row["password"] or "")
        if not password:
            raise KworkAccountStoreError("Saved Kwork password is missing")
        try:
            persona = json.loads(str(row["persona_json"] or "{}"))
        except json.JSONDecodeError as exc:
            raise KworkAccountStoreError("Saved Kwork account persona has an invalid JSON format") from exc
        if not isinstance(persona, dict):
            raise KworkAccountStoreError("Saved Kwork account persona has an invalid format")
        if not str(persona.get("persona_id") or "").strip():
            persona = KworkAccountStore._default_persona(str(row["registration_id"]))
        return StoredKworkAccount(
            registration_id=str(row["registration_id"]),
            email=str(row["email"]),
            username=str(row["username"]),
            user_type=int(row["user_type"]),
            mail_provider=str(row["mail_provider"]),
            status=str(row["status"]),
            created_at=str(row["created_at"]),
            registration_started_at=str(row["registration_started_at"]),
            activated_at=str(row["activated_at"]) if row["activated_at"] else None,
            signup_ip=str(row["signup_ip"]) if row["signup_ip"] else None,
            activation_ip=str(row["activation_ip"]) if row["activation_ip"] else None,
            registration_slot=int(row["registration_slot"]) if row["registration_slot"] is not None else None,
            registration_proxy_url=(
                str(row["registration_proxy_url"])
                if row["registration_proxy_url"]
                else (str(session.get("proxy_url") or "").strip() or None)
            ),
            cookie_count=int(row["cookie_count"] or 0),
            last_error=str(row["last_error"]) if row["last_error"] else None,
            market_enabled=bool(int(row["market_enabled"] if row["market_enabled"] is not None else 1)),
            persona=persona,
            password=password,
            session=session,
        )
