# Kwork Market Rebuild Journal

## 2026-07-10 - Objective and Baseline

### User goal

- Replace the 10-card market view with a rubric-scoped supply analysis that can
  cover the available Kwork API slices efficiently.
- Keep buyer-order discovery separate from supply analysis.
- Remove seller-profile cards as a primary market surface.
- Replace hard-coded popularity terms and generic recommendations with an LLM
  analysis over raw, rubric-scoped market evidence.
- Add a context-preserving Market Assistant that can answer follow-up questions
  and request narrowly scoped refreshes.

### Observed baseline

- Category 38 reports 13,700 listings, but current price, trust, terms, and
  repeat-seller metrics use a 10-card sample.
- The UI can simultaneously show demand as `skipped` while the separate buyer
  scout has successfully collected buyer lots, which is misleading.
- The buyer budget input is presented like a hard filter, while results can
  include lots above that budget and only the score is penalized.
- Repeated `active` buyer values need validation before they influence advice.

### Operating constraints

- Use confirmed Kwork API and existing web-state fallback paths.
- Keep the scan bounded, cacheable, resumable, and observable.
- Respect published access rules and stop/back off on protection or upstream
  failure signals. Do not bypass CAPTCHA, authentication, or access controls.
- Use no fixed category keywords or fixed offer ideas; all analysis must be
  derived from the selected rubric's collected evidence.

## 2026-07-10 - Supply Scanner and Market Assistant

### Implemented route and evidence contract

- Added `POST /api/kwork/market/supply-scan`. It uses the selected category and
  API-provided classifier tree only; it does not call the buyer-order endpoint.
- The output keeps `reported_category_total` separate from
  `observed_listing_count`, records every requested page, and labels seller
  duplication as `seller_repetition_in_observed_cards`. It does not calculate
  or expose a market-concentration metric from the sample.
- Each normalized listing now retains its public source API card as `api_card`
  so the LLM receives evidence rather than a keyword/regex summary.
- The analysis and Market Assistant now accept up to 5,000 collected cards by
  default. `KWORK_MARKET_LLM_MAX_EVIDENCE_CHARS` bounds the serialized evidence
  window (default 3.5M characters) and the response reports exactly how many
  raw cards reached the model.
- The default target is at least 500 observed cards. `KWORK_MARKET_SUPPLY_MIN_CARDS`
  allows 500 through 5,000, while `KWORK_MARKET_SUPPLY_MAX_REQUESTS` defaults
  to 72 and may be explicitly raised to 720. The response exposes the estimated
  request count and whether the current request budget can reach the target.

### Collection methodology

- Pages are selected round-robin over API classifier slices, cache for 15
  minutes, and run in small waves. Defaults are two concurrent requests and a
  750 ms inter-wave delay; these are configurable as scanner settings. A single
  transport slot is serialized, so parallelism is distributed across configured
  local proxy ports rather than doubled through one port.
- A transient upstream error slows the next wave with exponential backoff plus
  jitter. If the error exposes `Retry-After` text, that delay is honored.
- A 429, 403, CAPTCHA, rate-limit, or access-denied signal stops the run and is
  returned explicitly in coverage metadata. The scanner does not solve CAPTCHA
  or bypass login/access controls.
- The scanner can use the project's existing configured transport pool. It reads
  `KWORK_MARKET_SUPPLY_PROXY_URLS`, `KWORK_PROXY_LIST`, or an explicit local
  port range in `KWORK_MARKET_SUPPLY_PROXY_PORTS`; it does not provision or
  manage proxy infrastructure itself. The current VPNTE control default is
  `http://127.0.0.1:17873`; the supplied local pool may be configured as
  `17990-17999`.
- The local `.env` now activates that supply pool with
  `KWORK_MARKET_SUPPLY_PROXY_PORTS=17990-17999`, while retaining the existing
  VPNTE control configuration.
- This policy follows the HTTP convention that clients should respect 429 and
  `Retry-After`, and avoids repeat fetches through its page cache. The current
  Kwork client does not expose ETag or Last-Modified validators, so conditional
  revalidation is not claimed yet. Sources consulted through Tavily search, not Tavily
  research: [RFC 6585](https://www.rfc-editor.org/rfc/rfc6585) / RFC 9110
  guidance on 429 and Retry-After; [RFC 7232](https://www.rfc-editor.org/rfc/rfc7232)
  on conditional requests; [RFC 9309](https://www.rfc-editor.org/rfc/rfc9309)
  on crawler caching.

### UI and assistant separation

- The primary action is now `Анализ предложений`; it invokes only the new
  rubric-scoped supply scanner. `Поиск заказов` is a separate action with its
  own budget limit and cannot affect supply metrics.
- The legacy `Заказы: skipped` tile and legacy ten-card trust/concentration
  panel are hidden from the primary path. The new result surface presents full
  category volume, observed cards, scanned slices, coverage confidence, price
  sample, and the actual cards separately.
- Seller profile cards are not part of the new supply surface.
- The Market Assistant persists the scan context and may refresh only one
  already-scanned classifier for one to three pages. Its follow-up answer is
  regenerated with that fresh evidence, which remains in subsequent chat turns.

### Validation

- Added isolated tests for separated totals versus observations, protection
  stop behavior, and assistant narrow refresh.
- Server validation run: `66 passed` across `test_kwork_market.py`,
  `test_kwork_routes.py`, `test_kwork_market_supply.py`, and
  `test_vpnte_proxy.py`.
- Browser/UI verification was deliberately not run for this iteration.
