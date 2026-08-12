"""Account-bound read, sync, draft, and explicit-send APIs for Buyer Search conversations.

Every conversation key is assembled from the fixed Kwork platform, the
account in the URL, and the remote dialog ID in the URL, so an identical
dialog on a second account cannot be read through the first account's path.
The sync route has read-only capabilities only.  The narrow draft-send route
requires a separately injected account-pinned controller and an explicit
operator confirmation; no route creates background delivery work.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from src.platforms.kwork_buyer.conversation_copilot import (
    BuyerConversationCopilotConflictError,
    BuyerConversationCopilotController,
    BuyerConversationCopilotError,
    BuyerConversationCopilotNotFoundError,
)
from src.platforms.kwork_buyer.conversation_persistence import (
    BuyerConversationPersistenceConflictError,
    BuyerConversationPersistenceError,
    BuyerConversationPersistenceNotFoundError,
    SQLiteBuyerConversationStore,
)
from src.platforms.kwork_buyer.conversation_sync import (
    BuyerConversationSyncCapabilityError,
    BuyerConversationSyncController,
    BuyerConversationSyncDisabledError,
    BuyerConversationSyncError,
)
from src.platforms.kwork_buyer.conversation_send import (
    BuyerConversationRemoteSendReceipt,
    BuyerConversationSendCapabilityError,
    BuyerConversationSendConflictError,
    BuyerConversationSendController,
    BuyerConversationSendDisabledError,
    BuyerConversationSendError,
    BuyerConversationSendNotFoundError,
)
from src.platforms.kwork_buyer.conversations import (
    BuyerConversationContext,
    BuyerConversationDeliveryState,
    BuyerConversationError,
    BuyerConversationKey,
    create_outgoing_draft,
)


router = APIRouter(
    prefix="/api/kwork/buyer-search/accounts/{account_registration_id}",
    tags=["kwork-buyer-conversations"],
)


class BuyerConversationContextRequest(BaseModel):
    """Optional durable project and proposal links for one editor draft."""

    model_config = ConfigDict(extra="forbid")

    project_id: str | None = Field(default=None, max_length=200)
    proposal_draft_id: str | None = Field(default=None, max_length=200)
    proposal_intent_id: str | None = Field(default=None, max_length=200)
    buyer_remote_user_id: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_domain(self) -> BuyerConversationContext:
        return BuyerConversationContext(**self.model_dump())


class BuyerConversationDraftCreateRequest(BaseModel):
    """Local editor content only; sender identity always comes from the path."""

    model_config = ConfigDict(extra="forbid")

    draft_id: str | None = Field(default=None, min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=20_000)
    context: BuyerConversationContextRequest = Field(default_factory=BuyerConversationContextRequest)
    created_at: str | None = Field(default=None, max_length=100)
    source: str = Field(default="operator", min_length=1, max_length=100)

    @field_validator("body")
    @classmethod
    def require_nonblank_body(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("body cannot be blank")
        return value


class BuyerConversationCopilotDraftRequest(BaseModel):
    """Parameters for an AI-generated local draft, never an outgoing message."""

    model_config = ConfigDict(extra="forbid")

    draft_id: str | None = Field(default=None, min_length=1, max_length=200)
    operator_instruction: str | None = Field(default=None, max_length=8_000)
    created_at: str | None = Field(default=None, max_length=100)


class BuyerConversationSyncRequest(BaseModel):
    """Bounded parameters for an account-scoped, read-only inbox sync."""

    model_config = ConfigDict(extra="forbid")

    dialog_limit: int = Field(default=100, ge=1, le=200)
    message_limit: int = Field(default=500, ge=1, le=1_000)


class BuyerConversationDraftSendRequest(BaseModel):
    """The body is fixed by the saved draft; only an operator can command delivery."""

    model_config = ConfigDict(extra="forbid")

    requested_by: str = Field(..., min_length=1, max_length=200)
    command_id: str = Field(..., min_length=1, max_length=200)
    confirm_send: StrictBool


class BuyerConversationSendReconcileRequest(BaseModel):
    """Explicit remote evidence used to resolve an already-unknown send outcome."""

    model_config = ConfigDict(extra="forbid")

    requested_by: str = Field(..., min_length=1, max_length=200)
    command_id: str = Field(..., min_length=1, max_length=200)
    confirm_reconciliation: StrictBool
    accepted: StrictBool | None = None
    remote_message_id: str | None = Field(default=None, max_length=300)
    remote_receipt: str | None = Field(default=None, max_length=500)
    delivery_state: BuyerConversationDeliveryState = BuyerConversationDeliveryState.SENT
    observed_at: str | None = Field(default=None, max_length=100)
    failure_reason: str | None = Field(default=None, max_length=1_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_receipt(self) -> BuyerConversationRemoteSendReceipt:
        accepted = self.accepted
        if accepted is None and self.remote_message_id is not None:
            accepted = True
        return BuyerConversationRemoteSendReceipt(
            accepted=accepted,
            remote_message_id=self.remote_message_id,
            remote_receipt=self.remote_receipt,
            delivery_state=self.delivery_state,
            observed_at=self.observed_at,
            metadata=self.metadata,
        )


def _store(app: Any) -> SQLiteBuyerConversationStore:
    store = getattr(app.state, "buyer_conversations", None)
    if not isinstance(store, SQLiteBuyerConversationStore):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Buyer conversations are unavailable",
        )
    return store


def _copilot(app: Any) -> BuyerConversationCopilotController:
    controller = getattr(app.state, "buyer_conversation_copilot", None)
    if not isinstance(controller, BuyerConversationCopilotController):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Buyer conversation copilot is unavailable",
        )
    return controller


def _sync_controller(app: Any) -> BuyerConversationSyncController:
    controller = getattr(app.state, "buyer_conversation_sync", None)
    if not isinstance(controller, BuyerConversationSyncController):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Buyer conversation sync is unavailable",
        )
    return controller


def _send_controller(app: Any) -> BuyerConversationSendController:
    controller = getattr(app.state, "buyer_conversation_send", None)
    if not isinstance(controller, BuyerConversationSendController):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Buyer conversation send is unavailable",
        )
    return controller


async def _call(operation: Any) -> Any:
    try:
        return await operation
    except BuyerConversationPersistenceNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except BuyerConversationPersistenceConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (BuyerConversationPersistenceError, BuyerConversationError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


async def _call_copilot(operation: Any) -> Any:
    try:
        return await operation
    except BuyerConversationCopilotNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except BuyerConversationCopilotConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except BuyerConversationCopilotError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


async def _call_sync(operation: Any) -> Any:
    try:
        return await operation
    except (BuyerConversationSyncDisabledError, BuyerConversationSyncCapabilityError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except BuyerConversationSyncError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


async def _call_send(operation: Any) -> Any:
    try:
        return await operation
    except BuyerConversationSendNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (
        BuyerConversationSendConflictError,
        BuyerConversationSendDisabledError,
        BuyerConversationSendCapabilityError,
    ) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except BuyerConversationSendError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


def _not_found(kind: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Buyer conversation {kind} was not found")


async def _key_for(
    account_registration_id: str,
    remote_dialog_id: str,
) -> BuyerConversationKey:
    """Construct the scope-bound key inside ``_call`` error handling."""

    return BuyerConversationKey(
        platform="kwork",
        account_registration_id=account_registration_id,
        remote_dialog_id=remote_dialog_id,
    )


async def _load_key(
    account_registration_id: str,
    remote_dialog_id: str,
) -> BuyerConversationKey:
    return await _call(_key_for(account_registration_id, remote_dialog_id))


@router.get("/conversation-cursor")
async def get_conversation_cursor(account_registration_id: str, request: Request) -> dict[str, Any]:
    cursor = await _call(
        _store(request.app).get_cursor(
            platform="kwork",
            account_registration_id=account_registration_id,
        )
    )
    return {"cursor": cursor.to_payload() if cursor is not None else None}


@router.get("/conversations")
async def list_conversations(
    account_registration_id: str,
    request: Request,
    limit: int = 200,
) -> dict[str, list[dict[str, Any]]]:
    conversations = await _call(
        _store(request.app).list_conversations(
            platform="kwork",
            account_registration_id=account_registration_id,
            limit=limit,
        )
    )
    return {"items": [conversation.to_payload() for conversation in conversations]}


@router.post("/conversations/sync")
async def sync_conversations(
    account_registration_id: str,
    request: Request,
    payload: BuyerConversationSyncRequest | None = None,
) -> dict[str, Any]:
    """Read and atomically persist one account-bound inbox snapshot."""

    params = payload or BuyerConversationSyncRequest()
    return await _call_sync(
        _sync_controller(request.app).sync_account(
            account_registration_id=account_registration_id,
            dialog_limit=params.dialog_limit,
            message_limit=params.message_limit,
        )
    )


@router.get("/conversations/{remote_dialog_id}")
async def get_conversation(
    account_registration_id: str,
    remote_dialog_id: str,
    request: Request,
) -> dict[str, Any]:
    key = await _load_key(account_registration_id, remote_dialog_id)
    conversation = await _call(_store(request.app).get_conversation(key))
    if conversation is None:
        raise _not_found("dialog")
    return conversation.to_payload()


@router.get("/conversations/{remote_dialog_id}/messages")
async def list_messages(
    account_registration_id: str,
    remote_dialog_id: str,
    request: Request,
    limit: int = 500,
) -> dict[str, list[dict[str, Any]]]:
    key = await _load_key(account_registration_id, remote_dialog_id)
    messages = await _call(_store(request.app).list_messages(key=key, limit=limit))
    return {"items": [message.to_payload() for message in messages]}


@router.get("/conversations/{remote_dialog_id}/messages/{remote_message_id}")
async def get_message(
    account_registration_id: str,
    remote_dialog_id: str,
    remote_message_id: str,
    request: Request,
) -> dict[str, Any]:
    key = await _load_key(account_registration_id, remote_dialog_id)
    message = await _call(_store(request.app).get_message(key=key, remote_message_id=remote_message_id))
    if message is None:
        raise _not_found("message")
    return message.to_payload()


@router.get("/conversations/{remote_dialog_id}/drafts")
async def list_drafts(
    account_registration_id: str,
    remote_dialog_id: str,
    request: Request,
    limit: int = 200,
) -> dict[str, list[dict[str, Any]]]:
    key = await _load_key(account_registration_id, remote_dialog_id)
    drafts = await _call(_store(request.app).list_drafts(key=key, limit=limit))
    return {"items": [draft.to_payload() for draft in drafts]}


async def _create_draft(
    store: SQLiteBuyerConversationStore,
    *,
    account_registration_id: str,
    remote_dialog_id: str,
    payload: BuyerConversationDraftCreateRequest,
) -> dict[str, Any]:
    key = BuyerConversationKey(
        platform="kwork",
        account_registration_id=account_registration_id,
        remote_dialog_id=remote_dialog_id,
    )
    draft = create_outgoing_draft(
        draft_id=payload.draft_id or f"buyer-reply-{uuid4()}",
        key=key,
        sender_account_registration_id=account_registration_id,
        body=payload.body,
        context=payload.context.to_domain(),
        created_at=payload.created_at,
        source=payload.source,
    )
    return (await store.save_draft(draft)).to_payload()


@router.post("/conversations/{remote_dialog_id}/drafts", status_code=status.HTTP_201_CREATED)
async def create_draft(
    account_registration_id: str,
    remote_dialog_id: str,
    payload: BuyerConversationDraftCreateRequest,
    request: Request,
) -> dict[str, Any]:
    return await _call(
        _create_draft(
            _store(request.app),
            account_registration_id=account_registration_id,
            remote_dialog_id=remote_dialog_id,
            payload=payload,
        )
    )


@router.post("/conversations/{remote_dialog_id}/drafts/{draft_id}/send")
async def send_draft(
    account_registration_id: str,
    remote_dialog_id: str,
    draft_id: str,
    payload: BuyerConversationDraftSendRequest,
    request: Request,
) -> dict[str, Any]:
    """Issue one explicit, account-pinned remote delivery for a saved local draft."""

    key = await _load_key(account_registration_id, remote_dialog_id)
    return await _call_send(
        _send_controller(request.app).send_draft(
            key=key,
            draft_id=draft_id,
            requested_by=payload.requested_by,
            command_id=payload.command_id,
            explicit_operator_command=payload.confirm_send,
        )
    )


@router.get("/conversations/{remote_dialog_id}/send-intents/{intent_id}")
async def get_send_intent(
    account_registration_id: str,
    remote_dialog_id: str,
    intent_id: str,
    request: Request,
) -> dict[str, Any]:
    """Inspect a durable send record without triggering another remote request."""

    key = await _load_key(account_registration_id, remote_dialog_id)
    return await _call_send(_send_controller(request.app).get_send_intent(key=key, intent_id=intent_id))


@router.get("/conversations/{remote_dialog_id}/send-intents/{intent_id}/audit")
async def list_send_audit(
    account_registration_id: str,
    remote_dialog_id: str,
    intent_id: str,
    request: Request,
    limit: int = 200,
) -> dict[str, list[dict[str, Any]]]:
    """Read durable send evidence without re-dispatching the local reply draft."""

    key = await _load_key(account_registration_id, remote_dialog_id)
    return await _call_send(
        _send_controller(request.app).list_send_audit(key=key, intent_id=intent_id, limit=limit)
    )


@router.post("/conversations/{remote_dialog_id}/send-intents/{intent_id}/reconcile")
async def reconcile_send_intent(
    account_registration_id: str,
    remote_dialog_id: str,
    intent_id: str,
    payload: BuyerConversationSendReconcileRequest,
    request: Request,
) -> dict[str, Any]:
    """Persist explicit remote receipt evidence without re-dispatching the draft."""

    key = await _load_key(account_registration_id, remote_dialog_id)
    return await _call_send(
        _send_controller(request.app).reconcile_send_intent(
            key=key,
            intent_id=intent_id,
            requested_by=payload.requested_by,
            command_id=payload.command_id,
            explicit_operator_command=payload.confirm_reconciliation,
            receipt=payload.to_receipt(),
            failure_reason=payload.failure_reason,
        )
    )


@router.post("/conversations/{remote_dialog_id}/reply-drafts", status_code=status.HTTP_201_CREATED)
async def create_copilot_draft(
    account_registration_id: str,
    remote_dialog_id: str,
    payload: BuyerConversationCopilotDraftRequest,
    request: Request,
) -> dict[str, Any]:
    """Generate and persist an immutable editor draft; no message is sent."""

    key = await _load_key(account_registration_id, remote_dialog_id)
    result = await _call_copilot(
        _copilot(request.app).generate_reply_draft(
            key=key,
            draft_id=payload.draft_id,
            operator_instruction=payload.operator_instruction,
            created_at=payload.created_at,
        )
    )
    return result.to_payload()


@router.get("/conversations/{remote_dialog_id}/drafts/{draft_id}")
async def get_draft(
    account_registration_id: str,
    remote_dialog_id: str,
    draft_id: str,
    request: Request,
) -> dict[str, Any]:
    key = await _load_key(account_registration_id, remote_dialog_id)
    draft = await _call(_store(request.app).get_draft(key=key, draft_id=draft_id))
    if draft is None:
        raise _not_found("draft")
    return draft.to_payload()


__all__ = [
    "BuyerConversationContextRequest",
    "BuyerConversationDraftCreateRequest",
    "BuyerConversationDraftSendRequest",
    "BuyerConversationSendReconcileRequest",
    "router",
]
