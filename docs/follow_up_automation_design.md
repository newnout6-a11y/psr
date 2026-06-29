# FOLLOW-UP AUTOMATION SYSTEM — Design Document

> **ROI**: 2-3x response rate increase (research consensus: 80% of deals require 5+ touches, most freelancers give up after 1)
> **Status**: Design phase
> **Date**: 2026-06-28

---

## TABLE OF CONTENTS

1. [Research Findings](#1-research-findings)
2. [Timing Strategy](#2-timing-strategy)
3. [Message Design](#3-message-design)
4. [Kwork-Specific Constraints](#4-kwork-specific-constraints)
5. [Database Schema](#5-database-schema)
6. [Method Signatures](#6-method-signatures)
7. [Orchestrator Integration](#7-orchestrator-integration)
8. [Telegram UX](#8-telegram-ux)
9. [Analytics](#9-analytics)
10. [Configuration](#10-configuration)
11. [Implementation Plan](#11-implementation-plan)

---

## 1. RESEARCH FINDINGS

### 1.1 Timing Consensus (5 sources)

| Source | First FU | Second FU | Final Nudge | Max Total |
|---|---|---|---|---|
| Plutio (2026) | 3-5 business days | 10 business days | 15-20 business days | 3 |
| Instantly.ai | Day 2-3 | Day 7 | Day 21-25 | 4 |
| Atlassian/Daylite | Day 3 | Day 6 | Day 13 | 3 |
| Gigradar (Upwork) | T+24h | T+72h | T+5-7d | 3 |
| LinkedIn (freelance) | 1 week | 2 weeks | 3-4 weeks | 3-4 |

**Consensus**: First follow-up at **48h** (sweet spot between "too eager" and "forgot about you"). Final nudge at **7 days** after last follow-up. Maximum **2 follow-ups** before graceful close.

### 1.2 Message Design Principles

From research (jovancicmil.com, mixmax.com, fyxer.com, gigradar.io):

- **NEVER** just "checking in" or "circling back" — adds zero value, feels spammy
- **ALWAYS** add new value: a new insight, a mini-asset, a relevant observation
- Each follow-up should stand alone — if the client only reads message #2, they should still want to respond
- Include ONE specific, answerable question (not "any questions?" but "would a 3-day prototype with X help you decide?")
- Keep it short — 2-3 sentences for chat (not email-length paragraphs)
- Reference the original proposal briefly but don't repeat it
- Avoid desperation signals: no discount offers, no "worried about no response", no "are you still interested?"
- Use the client's own words from the project description to show you read it

### 1.3 Kwork Platform Rules

From kwork.com ToS and community research:

- **Chat messages via inboxCreate**: ALLOWED — the system already uses this (`KworkExtensions.send_message_to_client`)
- **Contact info exchange**: FORBIDDEN — no Telegram, phone, email in messages
- **Commission discussion**: FORBIDDEN — mentioning Kwork's cut is penalized
- **Free test work**: FORBIDDEN — offering free samples violates Kwork rules
- **Rate limits**: The system already has `RatePacer` (1.5-4s delay, 8 msgs/60s burst limit)
- **Connects**: Follow-up CHAT messages do NOT consume connects (only proposal submissions do)
- **Anti-spam**: No explicit anti-spam for chat messages found, but the RatePacer + human-like delays mitigate risk

---

## 2. TIMING STRATEGY

### 2.1 Default Schedule

```
T=0h      Proposal sent (auto_sent or manual_sent)
T=48h     Follow-up #1 (value-add: new insight about their project)
T=120h    Follow-up #2 (value-add: alternative approach or question)
T=192h    Graceful close (optional, keeps door open)
```

### 2.2 Rationale

- **48h for FU#1**: Kwork projects are often decided within 24-72h. Following up at 48h catches the window before the client finalizes but after they've had time to review proposals. Shorter feels pushy; longer means the project may already be filled.
- **120h (5 days) for FU#2**: If no response after 2 days, 5 days total gives the client space while staying in the decision window. Most Kwork projects are resolved within a week.
- **192h (8 days) for close**: The "breakup" email. Removes pressure, creates soft urgency ("I'll assume the timing isn't right"), sometimes triggers a response from guilt/loss aversion.

### 2.3 Maximum Follow-ups

**2 follow-ups + 1 graceful close = 3 total touches maximum.**

Beyond 3, conversion drops to near zero and spam risk increases. The system should mark candidates as `followup_exhausted` after the close.

### 2.4 Configurability

All timing is configurable via env vars (following existing `_env_int` pattern):

```python
FOLLOWUP_1_DELAY_HOURS=48      # First follow-up after proposal sent
FOLLOWUP_2_DELAY_HOURS=120     # Second follow-up after proposal sent
FOLLOWUP_CLOSE_DELAY_HOURS=192 # Graceful close after proposal sent
FOLLOWUP_MAX_TOUCHES=3         # Maximum follow-up messages (excluding close)
FOLLOWUP_ENABLED=true          # Master switch
FOLLOWUP_REQUIRE_APPROVAL=true # Require Telegram approval (false = auto-send)
FOLLOWUP_SKIP_IF_RESPONDED=true # Cancel pending follow-ups if client responds
FOLLOWUP_MIN_AI_SCORE=6        # Only follow up on candidates with AI score >= this
FOLLOWUP_MAX_OFFERS=30         # Skip follow-up if competition is extreme
```

### 2.5 Cancellation Conditions

A scheduled follow-up is automatically cancelled if ANY of these occur:
1. Client responded (inbox monitor detects a reply → cancel all pending follow-ups for that candidate)
2. Candidate status changes to `hired`, `declined`, `completed`, or `skipped`
3. Client explicitly rejects in chat (detected via inbox message keywords: "не подходит", "другой исполнитель", "already hired")
4. Project is no longer found on Kwork (project closed/filled)

---

## 3. MESSAGE DESIGN

### 3.1 Follow-up System Prompt (LLM)

A dedicated system prompt for follow-up generation, separate from the original proposal prompt:

```python
followup_system_prompt_ru = """
Ты пишешь FOLLOW-UP сообщение клиенту на Kwork, который не ответил на первоначальный отклик.

ЦЕЛЬ: заставить клиента ответить, добавив НОВУЮ ценность. НЕ повторяй первоначальный отклик.

ПРАВИЛА (СТРОГО!):
1. Начинай с "Здравствуйте!". Без вариантов.
2. НИКАКИХ СМАЙЛИК, ЭМОДЗИ, MARKDOWN. Только текст.
3. НИКАКИХ упоминаний: "вы не ответили", "я жду", "напоминаю", "следую up". Клиент знает что не ответил.
4. НИКАКИХ скидок, предложений бесплатной работы, торга ценой. Это выглядит отчаянно.
5. НИКАКИХ контактных данных (Telegram, телефон, email) — запрещено правилами Kwork.
6. СТРУКТУРА (2-3 предложения):
   а) Здравствуйте!
   б) НОВОЕ наблюдение/инсайт по проекту, которого не было в первоначальном отклике. Например: альтернативный технический подход, заметка о потенциальной проблеме, идея по оптимизации. Это должно показывать что ты продолжаешь думать над задачей.
   в) Конкретный вопрос, на который легко ответить. НЕ "есть ли вопросы?" — а конкретный: "какой формат данных вы планируете использовать?" или "подойдёт ли подход с X?"
7. ДЛИНА: 2-3 предложения. Не больше. Это чат, не email.
8. ТОНАЛЬНОСТЬ: спокойная, профессиональная, без давления. Ты эксперт который делится мыслью, не продавец который торопит.
"""
```

### 3.2 Follow-up Type Variants

Each follow-up touch has a different "angle" to avoid repetition:

| Touch | Type | Angle | Example Hook |
|---|---|---|---|
| FU #1 | `value_add` | New technical insight | "Пока думал над задачей — заметил что для X подойдёт Y вместо Z, это сократит время на N%" |
| FU #2 | `question` | Specific project question | "Уточните один момент по ТЗ: данные будут в формате X или Y? От этого зависит архитектура" |
| Close | `graceful_close` | Soft exit, door open | "Понимаю что время может быть неподходящим. Если что-то изменится — буду рад вернуться к обсуждению" |

### 3.3 LLM vs Template

**LLM-generated** is the primary path (consistent with the existing `ProposalGenerator` pattern). The system provides:
- Original project description
- Original proposal text (so LLM knows what was already said and avoids repetition)
- Conversation history (if any client messages exist)
- The follow-up type (value_add / question / graceful_close)

**Template fallback** if LLM is unavailable (same pattern as `proposal_generator.py:480`):

```python
FOLLOWUP_TEMPLATES = {
    "value_add": (
        "Здравствуйте! Продолжая думать над вашей задачей — "
        "обратил внимание что {insight}. "
        "{question}"
    ),
    "question": (
        "Здравствуйте! Уточните по проекту: {question_text}? "
        "От этого зависит подход к реализации."
    ),
    "graceful_close": (
        "Здравствуйте! Понимаю, что время может быть неподходящим. "
        "Если проект ещё актуален — буду рад продолжить обсуждение."
    ),
}
```

### 3.4 Anti-Spam Safeguards

The follow-up text passes through the same `_clean_llm_response()` and `_sanitize_text()` pipeline as the original proposal. Additional follow-up-specific checks:

- No mention of the original proposal text (diff check: if >60% overlap with original, regenerate)
- No mention of "не ответили" / "ожидание" / "напоминаю" patterns
- No contact info (regex scan — already exists in `_sanitize_text`)
- Length cap: 500 characters (chat messages should be short)

---

## 4. KWORK-SPECIFIC CONSTRAINTS

### 4.1 Chat vs New Proposal

**Follow-ups go in CHAT, not as new proposals.**

Rationale:
- New proposals consume connects (limited monthly resource, 20-80/month)
- Chat messages are free and unlimited
- The system already has `KworkService.send_message(user_id, text)` via `inboxCreate`
- Chat is the natural channel for follow-up communication

### 4.2 Rate Limiting

The existing `RatePacer` in `kwork_ext.py:32` provides:
- 1.5-4.0 second random delay between API calls
- Burst limit: 8 calls per 60-second window

Follow-up sending uses the same pacer. No additional rate limiting needed — follow-ups are sent at most a few per cycle.

### 4.3 Anti-Spam Risk Mitigation

| Risk | Mitigation |
|---|---|
| Message flagged as spam | RatePacer delays, human-like timing, no duplicate content |
| Account warning for aggressive messaging | Max 2 follow-ups per candidate, 48h+ spacing |
| Client reports spam | `FOLLOWUP_REQUIRE_APPROVAL=true` by default — human reviews every follow-up |
| Banned from chat | LLM-generated unique content per follow-up, no template repetition |

### 4.4 User ID Resolution

Follow-up sending requires the client's `user_id` for the `inboxCreate` endpoint. Resolution chain (reuses existing logic from `notifier.py:527-538`):

1. `candidate.client_context.client.user_id` (from API enrichment)
2. `candidate.platform_data.user.USERID` (from Kwork stateData)
3. `candidate.platform_data.user.id` (fallback)
4. If no user_id → skip follow-up, log warning

---

## 5. DATABASE SCHEMA

### 5.1 New Table: `follow_ups`

```sql
CREATE TABLE IF NOT EXISTS follow_ups (
    follow_up_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id      INTEGER NOT NULL,
    project_id        TEXT NOT NULL,
    platform          TEXT NOT NULL,
    touch_number      INTEGER NOT NULL DEFAULT 1,      -- 1, 2, or 3 (close)
    message_type      TEXT NOT NULL DEFAULT 'value_add', -- value_add | question | graceful_close
    status            TEXT NOT NULL DEFAULT 'scheduled', -- scheduled | pending_approval | approved | sent | skipped | cancelled | failed
    message_text      TEXT,                              -- LLM-generated follow-up text
    scheduled_at      TEXT NOT NULL,                     -- when it should be sent
    sent_at           TEXT,                              -- when it was actually sent
    approved_at       TEXT,                              -- when operator approved via Telegram
    approved_by       TEXT,                              -- 'telegram' | 'auto'
    response_received_at TEXT,                           -- when client responded after this FU (for analytics)
    response_text     TEXT,                              -- client's response text (if any)
    error_message     TEXT,                              -- send failure reason
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_follow_ups_status_scheduled ON follow_ups(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_follow_ups_candidate ON follow_ups(candidate_id);
CREATE INDEX IF NOT EXISTS idx_follow_ups_project ON follow_ups(project_id, platform);
```

### 5.2 Candidates Table: New Columns

```sql
-- Added via _ensure_column pattern:
ALTER TABLE candidates ADD COLUMN followup_count INTEGER DEFAULT 0;
ALTER TABLE candidates ADD COLUMN followup_eligible INTEGER DEFAULT 1;  -- 0 if manually excluded
ALTER TABLE candidates ADD COLUMN followup_exhausted INTEGER DEFAULT 0; -- 1 after graceful close sent
ALTER TABLE candidates ADD COLUMN followup_last_at TEXT;                -- timestamp of last FU sent
```

### 5.3 Schema Migration

Following the existing `_ensure_column` pattern in `proposal_db.py:50-53`:

```python
# In ProposalDB._init_db(), after existing _ensure_column calls:

for column, ddl in [
    ("followup_count", "INTEGER DEFAULT 0"),
    ("followup_eligible", "INTEGER DEFAULT 1"),
    ("followup_exhausted", "INTEGER DEFAULT 0"),
    ("followup_last_at", "TEXT"),
]:
    self._ensure_column(conn, "candidates", column, ddl)
```

### 5.4 Status Flow

```
scheduled → pending_approval → approved → sent
                ↓                  ↓
            skipped            failed
                ↓
            cancelled (client responded / status changed)
```

---

## 6. METHOD SIGNATURES

### 6.1 FollowUpManager (new class: `src/action/follow_up_manager.py`)

```python
class FollowUpManager:
    """Управление lifecycle-ом follow-up сообщений."""

    def __init__(self, db: ProposalDB, generator: ProposalGenerator):
        self.db = db
        self.generator = generator
        self._config = FollowUpConfig.from_env()

    async def schedule_follow_ups_for_sent(self, candidate_id: int) -> list[int]:
        """Create scheduled follow_up rows for a newly sent proposal.
        Called after candidate status becomes auto_sent/manual_sent.
        Returns list of created follow_up_id values.
        Checks eligibility: AI score, offers count, followup_eligible flag.
        Creates rows for FU#1, FU#2, and close with scheduled_at timestamps.
        """

    async def check_due_follow_ups(self) -> list[dict[str, Any]]:
        """Find all follow_ups where status='scheduled' AND scheduled_at <= now.
        Called at the start of each orchestrator cycle.
        For each due follow-up:
          1. Check cancellation conditions (client responded? status changed?)
          2. Generate follow-up text via LLM (or template fallback)
          3. If FOLLOWUP_REQUIRE_APPROVAL: set status='pending_approval', notify Telegram
          4. If auto-send: set status='approved', send immediately
        Returns list of follow-ups that need attention.
        """

    async def generate_follow_up_text(
        self,
        candidate: dict[str, Any],
        touch_number: int,
        message_type: str,
    ) -> str:
        """Generate follow-up message via LLM.
        Uses followup_system_prompt_ru + original project context + original proposal text.
        Ensures no overlap with original proposal.
        Falls back to template if LLM unavailable.
        """

    async def send_follow_up(self, follow_up_id: int, actor: str = "telegram") -> str:
        """Send a follow-up message via Kwork chat.
        Resolves user_id from candidate data.
        Calls KworkService.send_message(user_id, text).
        On success: set status='sent', sent_at=now, update candidate.followup_count.
        On failure: set status='failed', error_message.
        Returns result message string.
        """

    def cancel_follow_ups(self, candidate_id: int, reason: str = "") -> int:
        """Cancel all pending/scheduled follow-ups for a candidate.
        Called when: client responds, candidate hired/declined/completed, manual skip.
        Returns number of cancelled follow-ups.
        """

    def mark_response_received(self, candidate_id: int, response_text: str) -> None:
        """Mark the most recent sent follow-up as responded.
        Sets response_received_at and response_text for analytics.
        Cancels any remaining scheduled follow-ups.
        """

    def is_eligible_for_followup(self, candidate: dict[str, Any]) -> bool:
        """Check if a candidate should get follow-ups.
        Returns False if:
          - FOLLOWUP_ENABLED is false
          - candidate.followup_eligible == 0
          - candidate.followup_exhausted == 1
          - candidate.ai_score < FOLLOWUP_MIN_AI_SCORE
          - candidate.offers_count > FOLLOWUP_MAX_OFFERS
          - candidate.platform not in AUTO_SEND_PLATFORMS
          - no resolvable user_id for Kwork
        """
```

### 6.2 FollowUpConfig (dataclass)

```python
@dataclass
class FollowUpConfig:
    enabled: bool = True
    require_approval: bool = True
    delay_1_hours: int = 48
    delay_2_hours: int = 120
    delay_close_hours: int = 192
    max_touches: int = 3
    min_ai_score: int = 6
    max_offers: int = 30
    skip_if_responded: bool = True

    @classmethod
    def from_env(cls) -> "FollowUpConfig":
        return cls(
            enabled=os.getenv("FOLLOWUP_ENABLED", "true").lower() in {"1", "true", "yes", "on"},
            require_approval=os.getenv("FOLLOWUP_REQUIRE_APPROVAL", "true").lower() in {"1", "true", "yes", "on"},
            delay_1_hours=_env_int("FOLLOWUP_1_DELAY_HOURS", 48, low=12, high=168),
            delay_2_hours=_env_int("FOLLOWUP_2_DELAY_HOURS", 120, low=24, high=336),
            delay_close_hours=_env_int("FOLLOWUP_CLOSE_DELAY_HOURS", 192, low=48, high=504),
            max_touches=_env_int("FOLLOWUP_MAX_TOUCHES", 3, low=1, high=5),
            min_ai_score=_env_int("FOLLOWUP_MIN_AI_SCORE", 6, low=1, high=10),
            max_offers=_env_int("FOLLOWUP_MAX_OFFERS", 30, low=1, high=200),
            skip_if_responded=os.getenv("FOLLOWUP_SKIP_IF_RESPONDED", "true").lower() in {"1", "true", "yes", "on"},
        )
```

### 6.3 ProposalDB Extensions

```python
# In ProposalDB class:

def create_follow_up(
    self,
    candidate_id: int,
    project_id: str,
    platform: str,
    touch_number: int,
    message_type: str,
    scheduled_at: str,
) -> int:
    """Insert a new follow_up row with status='scheduled'."""

def get_due_follow_ups(self, limit: int = 20) -> list[dict[str, Any]]:
    """SELECT * FROM follow_ups WHERE status='scheduled' AND scheduled_at <= now
    ORDER BY scheduled_at ASC LIMIT ?"""

def get_follow_up(self, follow_up_id: int) -> dict[str, Any] | None:
    """Get a single follow_up row."""

def get_pending_approval_follow_ups(self) -> list[dict[str, Any]]:
    """SELECT * FROM follow_ups WHERE status='pending_approval' ORDER BY scheduled_at ASC"""

def update_follow_up_status(
    self,
    follow_up_id: int,
    status: str,
    *,
    message_text: str | None = None,
    sent_at: str | None = None,
    approved_at: str | None = None,
    approved_by: str | None = None,
    error_message: str | None = None,
    response_received_at: str | None = None,
    response_text: str | None = None,
) -> None:
    """Update follow_up row fields."""

def cancel_follow_ups_for_candidate(self, candidate_id: int, reason: str = "") -> int:
    """UPDATE follow_ups SET status='cancelled' WHERE candidate_id=? AND status IN ('scheduled','pending_approval')"""

def get_follow_ups_for_candidate(self, candidate_id: int) -> list[dict[str, Any]]:
    """Get all follow-ups for a candidate, ordered by touch_number."""

def increment_candidate_followup_count(self, candidate_id: int) -> None:
    """UPDATE candidates SET followup_count = followup_count + 1, followup_last_at = now WHERE candidate_id=?"""

def mark_followup_exhausted(self, candidate_id: int) -> None:
    """UPDATE candidates SET followup_exhausted = 1 WHERE candidate_id=?"""

def get_follow_up_analytics(self) -> dict[str, Any]:
    """Aggregate analytics query (see section 9)."""
```

---

## 7. ORCHESTRATOR INTEGRATION

### 7.1 Where in the Cycle

Follow-up processing happens at the **start of each cycle**, right after inbox monitoring and before project discovery:

```python
async def run_cycle(self, dry_run: bool = False, limit_per_platform: int = 5):
    # ... existing setup ...

    # STEP 1: Check inbox (existing)
    try:
        responses = await self.inbox_monitor.check_all()
        for response in responses:
            if response.get("project_id") and response.get("message"):
                self.db.mark_response(response["project_id"], response["message"])
                # NEW: Cancel follow-ups for responded candidates
                self.follow_up_manager.cancel_follow_ups(
                    candidate_id_from_response(response),
                    reason="client responded",
                )
    except Exception as e:
        logger.debug(f"Ошибка проверки входящих: {e}")

    # STEP 2: Process due follow-ups (NEW)
    if self.follow_up_manager._config.enabled:
        due = await self.follow_up_manager.check_due_follow_ups()
        logger.info(f"Follow-ups due: {len(due)}")

    # STEP 3: Continue with existing discovery flow...
    # ... parse, filter, score, vet, generate, send ...

    # STEP 4: After successful proposal send (NEW)
    # In execute_candidate_action(), after status becomes auto_sent/manual_sent:
    if status in {"auto_sent", "manual_sent"} and not dry_run:
        await self.follow_up_manager.schedule_follow_ups_for_sent(candidate_id)
```

### 7.2 Initialization in `FreelanceOrchestrator.__init__`

```python
# In __init__, after self.generator and self.notifier:
from src.action.follow_up_manager import FollowUpManager

self.follow_up_manager = FollowUpManager(
    db=self.db,
    generator=self.generator,  # reuse existing ProposalGenerator + RAG
)

# Bind to notifier for approval callbacks
self.notifier.bind_follow_up_executor(self.follow_up_manager)
```

### 7.3 Scheduling Trigger

Follow-ups are scheduled in `execute_candidate_action()` after successful send:

```python
# In execute_candidate_action(), after the successful send block:
if status in {"auto_sent", "manual_sent"} and not dry_run:
    # ... existing log_db.log_send, record_query_signal ...
    with suppress(Exception):
        await self.follow_up_manager.schedule_follow_ups_for_sent(candidate_id)
```

### 7.4 Inbox Monitor Integration

When `InboxMonitor._persist_message()` detects a client response, it should also cancel follow-ups:

```python
# In _persist_message(), after update_candidate_status:
if candidate_id:
    # Cancel any pending follow-ups — client responded
    self.db.cancel_follow_ups_for_candidate(candidate_id, reason="client responded")
    # Mark the last sent follow-up as responded (for analytics)
    self.db.update_follow_up_response(candidate_id, resp.get("message", ""))
```

---

## 8. TELEGRAM UX

### 8.1 Follow-up Approval Message Format

When a follow-up is due and requires approval, the notifier sends a message with context:

```
🔄 Follow-up #1 для проекта

📌 Парсер данных с сайта
🆔 ID: g3129019
💰 Бюджет: 5000 руб.
⏰ Отклик отправлен: 2 дня назад
💬 Ответов от клиента: нет

📝 Оригинальный отклик:
Здравствуйте! Для парсинга каталога подойдёт Python + BeautifulSoup...
[ truncated ]

🔄 Предлагаемый follow-up:
Здравствуйте! Продолжая думать над задачей — обратил внимание что для
динамического контента лучше использовать Playwright вместо requests.
Это надёжнее для JS-рендеринга. Какой объём страниц планируется парсить
ежедневно?

[ Send Follow-up ] [ Skip ] [ Edit text ] [ Snooze 24h ]
```

### 8.2 Inline Keyboard

```python
def _follow_up_keyboard(self, follow_up: dict[str, Any]) -> InlineKeyboardMarkup:
    fu_id = follow_up["follow_up_id"]
    buttons = [
        [InlineKeyboardButton(
            text="Отправить follow-up",
            callback_data=f"followup:{fu_id}:send",
        )],
        [
            InlineKeyboardButton(text="Пропустить", callback_data=f"followup:{fu_id}:skip"),
            InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"followup:{fu_id}:edit"),
        ],
        [InlineKeyboardButton(
            text="Отложить на 24ч",
            callback_data=f"followup:{fu_id}:snooze:1440",
        )],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
```

### 8.3 Callback Handler

New callback prefix `followup:` in the existing `handle_candidate_action` dispatcher (or a new handler):

```python
@self.dp.callback_query(F.data.startswith("followup:"))
async def handle_follow_up_action(callback: CallbackQuery):
    if self.admin_id and str(callback.from_user.id) != str(self.admin_id):
        await callback.answer("Не авторизован", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) < 3:
        await callback.answer("Некорректное действие", show_alert=True)
        return

    fu_id = int(parts[1])
    action = parts[2]
    follow_up = self.db.get_follow_up(fu_id)
    if not follow_up:
        await callback.answer("Follow-up не найден", show_alert=True)
        return

    if action == "send":
        await callback.answer("Отправляю...")
        result = await self._follow_up_executor.send_follow_up(fu_id, actor="telegram")
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(result)

    elif action == "skip":
        self.db.update_follow_up_status(fu_id, "skipped", approved_by="telegram")
        await callback.answer("Пропущено")
        await callback.message.edit_reply_markup(reply_markup=None)

    elif action == "edit":
        self._awaiting_followup_edit[str(callback.from_user.id)] = fu_id
        await callback.answer("Пришли новый текст")
        await callback.message.answer(
            f"Пришли новый текст follow-up для #{fu_id}. "
            f"2-3 предложения, без markdown."
        )

    elif action == "snooze":
        minutes = int(parts[3]) if len(parts) > 3 else 1440
        new_scheduled = datetime.now() + timedelta(minutes=minutes)
        self.db.update_follow_up_status(
            fu_id, "scheduled",
            # Reset to scheduled with new time
        )
        # Actually need to update scheduled_at
        self.db.reschedule_follow_up(fu_id, new_scheduled.strftime("%Y-%m-%d %H:%M:%S"))
        await callback.answer(f"Отложено на {minutes} мин.")
        await callback.message.edit_reply_markup(reply_markup=None)
```

### 8.4 Text Edit Handler

In the existing `handle_text_input` handler, add a new branch:

```python
# After _awaiting_msg handling, before _awaiting_text_edit:
fu_id = self._awaiting_followup_edit.pop(chat_id, None)
if fu_id:
    if len(text) < 20:
        await message.answer("Текст слишком короткий для follow-up.")
        self._awaiting_followup_edit[chat_id] = fu_id
        return
    self.db.update_follow_up_status(fu_id, "pending_approval", message_text=text)
    await message.answer(
        f"Текст follow-up #{fu_id} обновлён. Нажми 'Отправить' для отправки."
    )
    # Re-send the approval message with updated text
    follow_up = self.db.get_follow_up(fu_id)
    await self.notify_follow_up_approval(follow_up)
    return
```

### 8.5 New Notifier Method

```python
async def notify_follow_up_approval(
    self,
    follow_up: dict[str, Any],
    chat_id: str | None = None,
) -> None:
    """Send a follow-up approval request to the operator."""
    if not self.bot:
        return
    target = chat_id or self.admin_id
    if not target:
        return

    candidate = self.db.get_candidate(follow_up["candidate_id"])
    if not candidate:
        return

    # Get conversation history (client responses, if any)
    conv = self.db.get_conversation(
        follow_up["project_id"],
        follow_up["platform"],
    )
    client_messages = ""
    if conv and conv.get("messages"):
        client_msgs = [m for m in conv["messages"] if m.get("sender") == "customer"]
        if client_msgs:
            client_messages = "\n💬 Ответы клиента:\n" + "\n".join(
                f"  — {m['message_text'][:200]}" for m in client_msgs[-2:]
            )

    touch_label = {1: "#1", 2: "#2", 3: "закрывающее"}.get(
        follow_up["touch_number"], f"#{follow_up['touch_number']}"
    )

    text = (
        f"🔄 Follow-up {touch_label} для проекта\n\n"
        f"📌 {candidate.get('title', 'Без названия')}\n"
        f"🆔 ID: {follow_up['project_id']}\n"
        f"💰 Бюджет: {self._budget_int(candidate)} руб.\n"
        f"⏰ Отклик отправлен: {candidate.get('sent_at', '?')}\n"
        f"📝 Follow-ups отправлено: {candidate.get('followup_count', 0)}\n"
        f"{client_messages}\n\n"
        f"📝 Оригинальный отклик:\n{(candidate.get('proposal_text') or '')[:500]}\n\n"
        f"🔄 Предлагаемый follow-up:\n{follow_up.get('message_text', '')}\n\n"
    )
    keyboard = self._follow_up_keyboard(follow_up)
    await self._safe_send_message(target, text, reply_markup=keyboard)
```

### 8.6 New Commands

```
/followups        — List pending follow-up approvals
/followup_stats   — Show follow-up analytics summary
```

---

## 9. ANALYTICS

### 9.1 Tracked Metrics

| Metric | Query | Purpose |
|---|---|---|
| Follow-ups sent | `COUNT(*) WHERE status='sent'` | Volume tracking |
| Follow-up response rate | `COUNT(response_received_at IS NOT NULL) / COUNT(status='sent')` | Core effectiveness metric |
| Time-to-response after FU | `AVG(strftime(response_received_at) - strftime(sent_at))` | Speed metric |
| FU#1 vs FU#2 vs Close response rate | Group by touch_number | Which touch converts best |
| Response rate WITH follow-up vs WITHOUT | Compare candidates with followup_count>0 vs =0 | ROI proof |
| Follow-up skip rate | `COUNT(status='skipped') / COUNT(*)` | Operator trust metric |
| Follow-up failure rate | `COUNT(status='failed') / COUNT(*)` | Technical health |

### 9.2 ProposalDB.get_follow_up_analytics()

```python
def get_follow_up_analytics(self) -> dict[str, Any]:
    """Aggregate follow-up effectiveness metrics."""
    with self._connect() as conn:
        # Overall stats
        row = conn.execute("""
            SELECT
                COUNT(*) AS total,
                COUNT(CASE WHEN status='sent' THEN 1 END) AS sent,
                COUNT(CASE WHEN status='skipped' THEN 1 END) AS skipped,
                COUNT(CASE WHEN status='cancelled' THEN 1 END) AS cancelled,
                COUNT(CASE WHEN status='failed' THEN 1 END) AS failed,
                COUNT(CASE WHEN status='sent' AND response_received_at IS NOT NULL THEN 1 END) AS responded,
                COUNT(CASE WHEN status='pending_approval' THEN 1 END) AS pending_approval,
                COUNT(CASE WHEN status='scheduled' THEN 1 END) AS scheduled
            FROM follow_ups
        """).fetchone()

        # Per-touch breakdown
        touch_rows = conn.execute("""
            SELECT
                touch_number,
                message_type,
                COUNT(*) AS total,
                COUNT(CASE WHEN status='sent' THEN 1 END) AS sent,
                COUNT(CASE WHEN status='sent' AND response_received_at IS NOT NULL THEN 1 END) AS responded
            FROM follow_ups
            GROUP BY touch_number, message_type
            ORDER BY touch_number
        """).fetchall()

        # Comparison: with vs without follow-up
        comparison = conn.execute("""
            SELECT
                CASE WHEN c.followup_count > 0 THEN 'with_fu' ELSE 'no_fu' END AS group_label,
                COUNT(*) AS total_candidates,
                COUNT(CASE WHEN c.status IN ('hired','completed') THEN 1 END) AS hired,
                COUNT(CASE WHEN EXISTS(
                    SELECT 1 FROM conversations conv
                    WHERE conv.candidate_id = c.candidate_id
                      AND conv.status != 'new'
                ) THEN 1 END) AS responded
            FROM candidates c
            WHERE c.status IN ('auto_sent', 'manual_sent', 'hired', 'declined', 'completed')
            GROUP BY group_label
        """).fetchall()

        sent = int(row["sent"] or 0)
        responded = int(row["responded"] or 0)
        response_rate = (responded / sent * 100) if sent > 0 else 0

        return {
            "total": int(row["total"] or 0),
            "sent": sent,
            "skipped": int(row["skipped"] or 0),
            "cancelled": int(row["cancelled"] or 0),
            "failed": int(row["failed"] or 0),
            "responded": responded,
            "pending_approval": int(row["pending_approval"] or 0),
            "scheduled": int(row["scheduled"] or 0),
            "response_rate_pct": round(response_rate, 1),
            "per_touch": [dict(r) for r in touch_rows],
            "comparison": [dict(r) for r in comparison],
        }
```

### 9.3 Telegram Analytics Display

```
/followup_stats:

📊 Follow-up аналитика:
  Всего: 45
  Отправлено: 32
  Ответов получено: 11 (34.4%)
  Пропущено оператором: 8
  Отменено (клиент ответил сам): 5
  
  По касаниям:
    #1 (value_add): отправлено 18, ответов 7 (38.9%)
    #2 (question): отправлено 11, ответов 3 (27.3%)
    Close (graceful): отправлено 3, ответов 1 (33.3%)
  
  Сравнение:
    С follow-up: 32 кандидата, наймов 6 (18.8%)
    Без follow-up: 89 кандидатов, наймов 5 (5.6%)
    Прирост: +13.2 процентных пункта (3.4x)
```

---

## 10. CONFIGURATION

### 10.1 Environment Variables

```ini
# Follow-up automation
FOLLOWUP_ENABLED=true
FOLLOWUP_REQUIRE_APPROVAL=true
FOLLOWUP_1_DELAY_HOURS=48
FOLLOWUP_2_DELAY_HOURS=120
FOLLOWUP_CLOSE_DELAY_HOURS=192
FOLLOWUP_MAX_TOUCHES=3
FOLLOWUP_MIN_AI_SCORE=6
FOLLOWUP_MAX_OFFERS=30
FOLLOWUP_SKIP_IF_RESPONDED=true

# LLM for follow-up generation (reuse proposal provider by default)
FOLLOWUP_LLM_PROVIDER=auto
FOLLOWUP_LLM_TEMPERATURE=0.8
```

### 10.2 Runtime Toggle

Follow the existing runtime_state pattern for on/off switching via Telegram:

```python
# In TelegramNotifier, add /followup_on and /followup_off commands:
@self.dp.message(Command("followup_on"))
async def cmd_followup_on(message):
    self.db.set_runtime_state("followup.enabled", "1")
    await message.answer("Follow-up автоматизация включена.")

@self.dp.message(Command("followup_off"))
async def cmd_followup_off(message):
    self.db.set_runtime_state("followup.enabled", "0")
    await message.answer("Follow-up автоматизация выключена.")
```

---

## 11. IMPLEMENTATION PLAN

### Phase 1: Core Infrastructure (1-2 days)

1. **`proposal_db.py`**: Add `follow_ups` table + `_ensure_column` migrations for candidates
2. **`proposal_db.py`**: Add all follow-up DB methods (section 6.3)
3. **`follow_up_manager.py`**: Create `FollowUpManager` class with `FollowUpConfig`
4. **`proposal_generator.py`**: Add `generate_follow_up()` method with followup system prompt

### Phase 2: Orchestrator Integration (1 day)

5. **`orchestrator.py`**: Initialize `FollowUpManager` in `__init__`
6. **`orchestrator.py`**: Add `check_due_follow_ups()` call at cycle start (after inbox)
7. **`orchestrator.py`**: Add `schedule_follow_ups_for_sent()` call in `execute_candidate_action()`
8. **`inbox_monitor.py`**: Add `cancel_follow_ups_for_candidate()` on client response

### Phase 3: Telegram UX (1 day)

9. **`notifier.py`**: Add `notify_follow_up_approval()` method
10. **`notifier.py`**: Add `followup:` callback handler
11. **`notifier.py`**: Add follow-up text edit handler in `handle_text_input`
12. **`notifier.py`**: Add `/followups` and `/followup_stats` commands
13. **`notifier.py`**: Add `/followup_on` and `/followup_off` runtime toggle commands

### Phase 4: Analytics & Polish (1 day)

14. **`proposal_db.py`**: Add `get_follow_up_analytics()` method
15. **`notifier.py`**: Add `/followup_stats` formatted output
16. Testing: End-to-end cycle with a test candidate
17. Documentation: Update README with follow-up commands

### Total: ~4-5 days

---

## APPENDIX A: Follow-up Generation Prompt Assembly

```python
async def generate_follow_up_text(
    self,
    candidate: dict[str, Any],
    touch_number: int,
    message_type: str,
) -> str:
    from src.brain.llm_router import get_llm_router

    project_context = (
        f"ЗАКАЗ: {candidate.get('title', '')}\n"
        f"ОПИСАНИЕ: {candidate.get('description', '')[:1000]}\n"
        f"НАВЫКИ: {', '.join(candidate.get('skills', []))}\n"
    )

    original_proposal = candidate.get("proposal_text") or ""
    proposal_context = f"\nОРИГИНАЛЬНЫЙ ОТКЛИК (не повторяй его):\n{original_proposal[:800]}\n"

    # Conversation history (if client sent partial messages)
    conv = self.db.get_conversation(candidate["project_id"], candidate["platform"])
    conv_context = ""
    if conv and conv.get("messages"):
        client_msgs = [m for m in conv["messages"] if m.get("sender") == "customer"]
        if client_msgs:
            conv_context = "\nСООБЩЕНИЯ КЛИЕНТА:\n" + "\n".join(
                f"  — {m['message_text'][:300]}" for m in client_msgs
            )

    type_instruction = {
        "value_add": "Напиши НОВОЕ техническое наблюдение по проекту + конкретный вопрос.",
        "question": "Напиши уточняющий вопрос по ТЗ, который покажет что ты вник в детали.",
        "graceful_close": "Напиши мягкое прощальное сообщение, оставляя дверь открытой. Без давления.",
    }.get(message_type, "Напиши ценное дополнение к проекту + вопрос.")

    user_prompt = (
        f"{project_context}"
        f"{proposal_context}"
        f"{conv_context}\n"
        f"ТИП FOLLOW-UP: {message_type}\n"
        f"НОМЕР КАСАНИЯ: {touch_number}\n"
        f"ЗАДАЧА: {type_instruction}\n"
    )

    router = get_llm_router()
    provider = os.getenv("FOLLOWUP_LLM_PROVIDER", "auto")

    try:
        res = await router.generate(
            prompt=user_prompt,
            provider=provider if provider != "auto" else None,
            model=None,
            temperature=float(os.getenv("FOLLOWUP_LLM_TEMPERATURE", "0.8")),
            max_tokens=512,
            task="followup_writing",
            system_prompt=self.followup_system_prompt_ru,
        )
        if res:
            res = self.generator._clean_llm_response(res)
            # Verify no significant overlap with original
            if self._text_overlap(res, original_proposal) > 0.6:
                logger.warning("Follow-up text too similar to original, regenerating...")
                # One retry with explicit instruction
                res = await router.generate(
                    prompt=user_prompt + "\nВАЖНО: Не повторяй текст из оригинального отклика!",
                    provider=provider if provider != "auto" else None,
                    model=None,
                    temperature=0.9,
                    max_tokens=512,
                    task="followup_writing",
                    system_prompt=self.followup_system_prompt_ru,
                )
                if res:
                    res = self.generator._clean_llm_response(res)
            return res
    except Exception as e:
        logger.error(f"Follow-up generation error: {e}")

    # Template fallback
    return self._template_follow_up(message_type, candidate)

def _text_overlap(self, text_a: str, text_b: str) -> float:
    """Calculate Jaccard similarity between two texts."""
    if not text_a or not text_b:
        return 0.0
    words_a = set(text_a.lower().split())
    words_b = set(text_b.lower().split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)
```

## APPENDIX B: File Change Summary

| File | Changes |
|---|---|
| `src/action/proposal_db.py` | +follow_ups table, +candidate columns, +10 DB methods |
| `src/action/follow_up_manager.py` | **NEW** — FollowUpManager + FollowUpConfig |
| `src/action/proposal_generator.py` | +followup_system_prompt_ru, +generate_follow_up_text() |
| `src/orchestrator.py` | +FollowUpManager init, +check_due_follow_ups(), +schedule_follow_ups_for_sent() |
| `src/utils/inbox_monitor.py` | +cancel_follow_ups on response |
| `src/utils/notifier.py` | +notify_follow_up_approval(), +followup: handler, +/followups, +/followup_stats, +edit handler |
| `.env.example` | +FOLLOWUP_* env vars |
