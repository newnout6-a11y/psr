# Kwork backend code market analysis - 2026-07-09 09:56 MSK

Purpose: run the market analysis from backend code only, without the desktop UI or visual browser checks.

Artifacts:

- `docs/kwork_market_snapshots/backend_code_market_analysis_20260709T065632Z.json`
- `docs/kwork_market_snapshots/kwork_buyer_scout_20260709T065646Z.json`

Runtime state:

- Session Hub was live on `http://127.0.0.1:8669`.
- Session Hub returned `14` Kwork cookies for `kwork.ru`.
- VPNTE proxy was live on `http://127.0.0.1:17990`.
- The Python run explicitly loaded `C:\psr\.env`; without this, buyer APIs can silently return empty results because the token/session client is not initialized.

Buyer scout result:

- Probes: `10`.
- Unique buyer lots: `43`.
- Zero-offer lots: `8`.
- Low-offer lots (`0..5` offers): `32`.
- Top observed budget: `120000`.
- Median top budget: `15000`.
- Endpoint errors: none.

Probe performance and counts:

| Probe | Query | Filter | Count | Sample | Zero sample | Low sample | Time |
|---|---|---|---:|---:|---:|---:|---:|
| `telegram_bot_low_offer_ru` | `телеграм бот` | `kworks_filter_to=5` | 26 | 12 | 2 | 12 | 4333 ms |
| `telegram_bot_zero_ru` | `телеграм бот` | `kworks_filter_to=0` | 6 | 6 | 6 | 6 | 745 ms |
| `telegram_low_offer_en` | `telegram` | `kworks_filter_to=5` | 15 | 12 | 3 | 12 | 821 ms |
| `telegram_budget30_low_offer` | `telegram` | `price_from=30000`, `kworks_filter_to=5` | 10 | 10 | 2 | 10 | 1076 ms |
| `python_bot_low_offer_ru` | `python бот` | `kworks_filter_to=5` | 14 | 12 | 3 | 12 | 588 ms |
| `automation_low_offer_ru` | `автоматизация` | `kworks_filter_to=5` | 6 | 6 | 1 | 6 | 625 ms |
| `site_zero_ru` | `сайт` | `kworks_filter_to=0` | 4 | 4 | 4 | 4 | 824 ms |
| `site_rework_low_offer_ru` | `доработка сайта` | `kworks_filter_to=10` | 33 | 12 | 1 | 9 | 767 ms |
| `wordpress_low_offer` | `wordpress` | `kworks_filter_to=5` | 3 | 3 | 0 | 3 | 655 ms |
| `bitrix_low_offer` | `bitrix` | `kworks_filter_to=5` | 1 | 1 | 0 | 1 | 693 ms |

Top buyer lots observed:

| ID | Offers | Budget | Probe | Title |
|---:|---:|---:|---|---|
| 3202311 | 0 | 90000 | `telegram_bot_low_offer_ru` | Нужно создать 5-6 Telegram Stories с упоминанием |
| 3211575 | 0 | 120000 | `telegram_bot_zero_ru` | Поиск клиентов постоянно |
| 3192226 | 0 | 90000 | `telegram_bot_zero_ru` | Поиск заказов |
| 3204499 | 0 | 90000 | `telegram_bot_zero_ru` | 10000 упоминаний в telegram |
| 3212022 | 1 | 30000 | `telegram_low_offer_en` | Массовый инвайтинг +рассылка с отчетом |
| 3191485 | 1 | 90000 | `telegram_budget30_low_offer` | ТГ упоминания в сторис |
| 2214981 | 1 | 60000 | `python_bot_low_offer_ru` | Сделать бота или приложение |
| 3117473 | 1 | 45000 | `bitrix_low_offer` | Bitrix24 тех поддержка портала на 1 месяц |
| 3203050 | 2 | 90000 | `telegram_bot_low_offer_ru` | Массовые отметки пользователей в Telegram Stories |
| 3203045 | 1 | 99000 | `telegram_bot_low_offer_ru` | Отметки пользователей в Telegram Stories |

Supply snapshot:

| Segment | Kworks | Sample median price | Notes |
|---|---:|---:|---|
| Telegram classifier, category `46`, classifier `281` | 8772 | 1000 | Seller-side Telegram supply is large and cheap at the card level. |
| Programming broad, category `41` | 38156 | 1750 | Broad category is very saturated but still contains bot/automation cards. |
| Ready databases, category `113`, classifier `1117` | 34244 | 1500 | Huge supply; useful mainly for lead/database adjacent tasks. |
| Marketplaces, category `112`, classifier `1357` | 109403 | 750 | Extremely saturated and mostly not aligned with current Telegram/automation draft. |

Interpretation:

- The best actionable demand pocket is still Telegram/bot/automation, not the old seller-frequency chips.
- `телеграм бот` with `kworks_filter_to=0` is the cleanest "before others" lane: 6 live zero-offer lots.
- `telegram` with `price_from=30000` and `kworks_filter_to=5` is a strong paid lane: 10 live low-offer lots, 2 zero-offer lots, and it keeps high-budget tasks in the top set.
- `python бот` is also useful: 14 live low-offer lots, 3 zero-offer lots.
- `доработка сайта` has the largest raw count in this run, but the seller supply is broad and saturated; it is a secondary lane unless the draft is changed toward site support.
- `wordpress` and `bitrix` are narrow in this sample. They can be specialty probes, not main discovery lanes.

UI implication:

- The block like `OlegTixomirov · 2 карточки` is not market analysis. It is only repeated seller frequency among sampled seller cards.
- The real market-analysis block should lead with buyer lots: title, offers, budget, matched probe, score reasons, and direct project link.
- Any run that returns `unique_projects=0` while seller `/kworks` works should show "buyer auth/session missing" instead of implying there is no demand.

Confirmed API behavior:

- `POST /projects` and `POST /getWantsCount` are the fast buyer-market source.
- Useful parameters confirmed by this run: `categories`, `page`, `query`, `price_from`, `kworks_filter_to`.
- `POST /kworks` remains the fast seller/supply source for competitor pressure, but it is not enough to find buyers.
