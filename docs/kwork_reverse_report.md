# Kwork Reverse Report

Актуально для текущего PSR-аудита. Это пассивный reverse: публичные страницы,
локальные логи/SQLite, установленная библиотека `kwork==0.2.0` и уже имеющиеся
парсеры. Активный fuzzing/pentest Kwork не выполнялся.

## Sources

- `kwork` package:
  `C:\Users\Redmi\AppData\Local\Programs\Python\Python312\Lib\site-packages\kwork`
- PSR runtime data:
  `data/runtime/logs.db`, `data/runtime/proposals.db`, `data/runtime/logs.txt`
- PSR code:
  `src/platforms/kwork.py`, `src/parsers/kwork_api_parser.py`,
  `src/action/proposal_sender.py`

`hexstrike-ai` is an offensive MCP wrapper around scanners, fuzzers, crawlers,
browser automation, and vulnerability tooling. For PSR it is useful only as an
idea for local HAR/DOM/network artifact analysis. Do not run active scans
against Kwork.

## Mobile API: projects

The installed `kwork` library uses the mobile API host:

```text
https://api.kwork.ru/{endpoint}
```

Project search is implemented as:

```text
POST /projects
Authorization: Basic bW9iaWxlX2FwaTpxRnZmUmw3dw==
token=<mobile token>
categories=<comma separated ids>
price_from=<int>
price_to=<int>
hiring_from=<int>
kworks_filter_from=<int>
kworks_filter_to=<int>
page=<int>
query=<text>
```

The library parses `response` into `WantWorker` objects. Fields that matter for
PSR's money path:

- `id`
- `user_id`
- `username`
- `price`
- `title`
- `description`
- `offers`
- `parent_category_id`
- `category_id`
- `category_base_price`
- `user_projects_count`
- `user_hired_percent`
- `user_active_projects_count`
- `already_work`
- `allow_higher_price`
- `possible_price_limit`
- `user_need_portfolio`

Implication: `offers` and `user_hired_percent` must be first-class candidate
fields, not only transient `ProjectItem` attributes.

## Web Login

The library contains a stable web auth bridge:

```text
POST api.kwork.ru/getWebAuthToken
GET  kwork.ru/login-by-token?...  -> web cookies
GET  kwork.ru/<url_to_redirect>   -> additional cookies, e.g. csrf_user_token
```

This is a better primary path than browser clicking because it mirrors the
official mobile app WebView flow.

## Offer Submit Flow

`KworkWebClient.submit_exchange_offer()` performs:

```text
GET  /new_offer?project=<project_id>
POST /quick-faq/init
POST /wants/create_offer_draft
POST /projects/check_is_template
POST /api/offer/createoffer
```

Important form fields:

- `wantId`
- `offerType`
- `description`
- `kwork_duration`
- `kwork_price`
- `kwork_name`
- `csrftoken`
- `draftKey`

Implication: PSR should use `api.web.submit_exchange_offer()` first and keep the
browser flow only as fallback.

## Current PSR Findings

- `candidates` previously did not store `offers_count` and
  `client_hired_percent`, while Queue/Dashboard expected them.
- `DecisionPolicy.priority` previously behaved like risk weight: high-risk
  candidates could be sorted above low-risk candidates.
- `auto_ready` previously triggered immediate `execute_candidate_action(...,
  "auto_send")` inside `run_cycle`. In repaired semantics it means "ready for
  explicit approval".
- Competitor price scraping started a browser before proposal generation. This
  is slow and fragile; it is now opt-in via `SCRAPE_COMPETITOR_PRICES=true`.
- `src.evolution` previously imported `OriginFinder` and `APIReverser` with
  every `TLSClient` import. Those modules are now lazy exports and stay out of
  normal parser runtime.
- OSINT/probiv is not part of the default Kwork money path. Enable explicitly
  only when its signal is worth the extra latency and false positives.
