# Kwork buyer API filter matrix - 2026-07-09 02:36 MSK

Purpose: verify which buyer-search filters help find buyer lots faster than browsing the UI.

Artifacts:

- `docs/kwork_market_snapshots/kwork_buyer_filter_matrix_germany_20260708T233600Z.json`
- Earlier partial timeout artifacts:
  - `docs/kwork_market_snapshots/kwork_buyer_filter_matrix_short_20260708T232307Z.json`
  - `docs/kwork_market_snapshots/kwork_buyer_filter_matrix_retry_20260708T232525Z.json`

Proxy finding:

- `russia5`, `russiavless2`, and bridge profiles produced buyer API timeouts during this run.
- Explicit VPNTE rotate to `germanyvless1` restored normal responses.
- Working local proxy URL remained `http://127.0.0.1:17990`.

Confirmed buyer endpoints:

- `POST /projects`
  - Purpose: list buyer lots.
  - Auth: token/session API.
  - Useful params seen in code and live probes:
    - `categories`
    - `page`
    - `query`
    - `price_from`
    - `price_to`
    - `hiring_from`
    - `kworks_filter_from`
    - `kworks_filter_to`
- `POST /getWantsCount`
  - Purpose: fast count for a filter set.
  - Takes the same filter family.

Live matrix on `germanyvless1`:

- `telegram`, `kworks_filter_to=5`: count `16`, sample `12`.
- `telegram`, `kworks_filter_to=0`: count `3`, sample `3`.
- `telegram`, `price_from=30000`, `kworks_filter_to=5`: count `11`, sample `11`.
- `telegram`, `hiring_from=80`, `kworks_filter_to=5`: count `0`.
- `telegram`, `kworks_filter_to=5`, `sort=date`: count `16`, same first ids as default.
- `телеграм бот`, `kworks_filter_to=5`: count `22`, sample `12`.
- `сайт`, `kworks_filter_to=0`: count `6`, sample `6`.
- `доработка сайта`, `kworks_filter_to=10`: count `28`, sample `12`.
- category `41`, `kworks_filter_to=5`: count `6`, sample `6`.

Interpretation:

- `kworks_filter_to=0` is the cleanest "before others" signal.
- `kworks_filter_to=5` is the practical low-competition window with more volume.
- `price_from=30000` is useful for Telegram: it narrows from `16` to `11` without losing the strongest top ids in this sample.
- `hiring_from=80` is too strict for `telegram` in this run.
- `sort=date` is not proven useful: it returned the same count and first ids as the default `telegram <=5` query.

Implementation implication:

- Buyer scout should expose presets for:
  - zero-offer lots;
  - low-offer lots;
  - budget floor, especially `price_from=30000`;
  - query families like `телеграм бот`, `telegram`, `доработка сайта`, `python бот`.
- UI should not present seller frequency chips as market analysis.

Implementation update:

- `src/platforms/kwork_market.py`
  - Added default probe `telegram_budget30_low_offer`.
  - Raised `get_buyer_scout(..., max_probes=...)` default from `8` to `10`, so the new budget probe does not push zero-offer probes out of the default run.
- `src/api/routes/kwork.py`
  - Raised `KworkBuyerScoutRequest.max_probes` default from `8` to `10`.
- `tests/unit/test_kwork_market.py`
  - Added a regression test that checks the default buyer scout probes include the budget Telegram window and still include zero-offer probes.

Verification:

- `python -m pytest C:\psr\tests\unit\test_kwork_market.py C:\psr\tests\unit\test_kwork_routes.py -q`
  - Result: `37 passed`.
- `python -m ruff check C:\psr\src\platforms\kwork_market.py C:\psr\src\api\routes\kwork.py C:\psr\tests\unit\test_kwork_market.py`
  - Result: `All checks passed`.
