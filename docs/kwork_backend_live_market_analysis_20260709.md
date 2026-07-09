# Kwork backend live market analysis - 2026-07-09 02:08 MSK

Purpose: run a real market analysis from backend code only, without the desktop UI.

Artifacts:

- `docs/kwork_market_snapshots/backend_live_market_analysis_20260708T230828Z_utf8.json`
- `docs/kwork_market_snapshots/kwork_buyer_scout_20260708T230828Z.json`

Important implementation note:

- The first ad-hoc PowerShell stdin run corrupted Cyrillic probe strings into `????`.
- The repeated run passed Cyrillic queries as Unicode escapes, so Kwork received real values:
  - `телеграм бот`
  - `автоматизация`
  - `сайт`
  - `доработка сайта`
  - `python бот`

Seller supply snapshot:

- Programming broad: `38129` kworks, sampled price median `1000`, `/kworks` timing about `672 ms`.
- Telegram category: `8779` kworks, sampled price median `1000`, `/kworks` timing about `259 ms`.
- Website development broad: `38129` kworks, sampled price median `1000`, `/kworks` timing about `672 ms`.

Buyer scout snapshot:

- Probes: `8`.
- Unique projects: `39`.
- Zero-offer projects: `9`.
- Low-offer projects (`0..5` offers): `27`.
- Endpoint errors: none.
- Total buyer-scout timing: about `8-9 s` for 8 probes with details for top projects.

Probe results:

- `телеграм бот`, `<=5` offers: count `22`, sample `12`, zero-offer `3`, low-offer `9`.
- `telegram`, `<=5` offers: count `16`, sample `12`, zero-offer `2`, low-offer `10`.
- `автоматизация`, `<=5` offers: count `6`, sample `6`, zero-offer `2`, low-offer `4`.
- `сайт`, `0` offers: count `5`, sample `5`, zero-offer `5`.
- `доработка сайта`, `<=10` offers: count `29`, sample `12`, zero-offer `2`, low-offer `7`.
- `python бот`, `<=5` offers: count `8`, sample `8`, zero-offer `2`, low-offer `6`.
- `wordpress`, `<=5` offers: count `4`, sample `4`, low-offer `4`.
- `bitrix`, `<=5` offers: count `1`, sample `1`, low-offer `1`.

Top observed buyer lots:

- `3202311`: Telegram Stories mentions, `0` offers, price `30000`, possible limit `90000`, views `2662`.
- `3211575`: постоянный поиск клиентов на IT-разработку, `0` offers, price `40000`, possible limit `120000`.
- `3192226`: поиск заказов для сайтов/ботов/парсеров, `0` offers, price `30000`, possible limit `90000`.
- `3212022`: Telegram инвайтинг/рассылка, `1` offer, price `10000`, possible limit `30000`.
- `3117473`: Bitrix24 support for 1 month, `1` offer, price `15000`, possible limit `45000`.

Current reading:

- Best immediate demand pocket by zero-offer count: broad `сайт`, but many lots are lead-generation/partner-search tasks, not direct development.
- Best practical pocket for the current draft direction: `телеграм бот` / `telegram`, because it has meaningful count, many low-offer lots, and budgets around `10000..40000+`.
- `доработка сайта` has the biggest raw count in this probe set, but the sampled competition is higher and the broad seller supply is huge.
- `bitrix` has low competition in this sample but too little fresh count to treat as the main direction.

Clarification for UI:

- Chips like `OlegTixomirov · 2 карточки` are not market analysis.
- They are only seller frequency in the sampled seller cards.
- Real market analysis should be shown from buyer scout: ranked buyer lots, offer counts, budget, views, and matched probe.
