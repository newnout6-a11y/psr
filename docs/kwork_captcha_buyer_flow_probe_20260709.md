# Kwork captcha and buyer-flow probe - 2026-07-09 10:14 MSK

Purpose: investigate the user's captcha symptom without visual UI checks and identify buyer-flow endpoints useful before sending an offer.

Artifacts:

- `docs/kwork_market_snapshots/kwork_captcha_buyer_flow_probe_20260709T070826Z.json`

Runtime state:

- Session Hub was live on `http://127.0.0.1:8669`.
- Session Hub returned `14` Kwork cookies for `kwork.ru`.
- VPNTE proxy was live on `http://127.0.0.1:17990`.

Live endpoint results:

| Endpoint | Result | Usefulness |
|---|---|---|
| `POST /getCaptchaStatus` via mobile token API | `success=true`, `response.show_captcha=false` | Account health signal. It currently says no captcha is required. |
| `GET /new_offer?project=3202311` with Session Hub cookies | HTTP `200`, title `Предложить услугу - Kwork`, no `/not_access.php`, no `data-sitekey` | Page is reachable. Global SmartCaptcha scripts exist in HTML but are not enough to prove a challenge is active. |
| `GET /wants/3202311/check_offer_notify` with Session Hub cookies | HTTP `200`, `success=true`, category `46`, classification `281`, portfolio/kwork notify flags | Useful read-like preflight for a buyer lot before drafting an offer. |
| `POST /projects/check_is_template` with Session Hub cookies | HTTP `200`, `success=true`, `data=[]` | Useful read-like preflight to check whether a proposal text looks templated. |
| `POST /api/offer/addview` | Not retested | Stateful: marks want cards viewed. Keep out of passive market scans. |

Important bug found:

- `KworkExtensions.get_captcha_status()` treated any non-empty `response` object as `True`.
- Live Kwork returned `{"show_captcha": false}`.
- Before the fix, `bool({"show_captcha": false})` became `True`, so PSR could report captcha even when Kwork said no captcha was needed.

Fix applied:

- `src/platforms/kwork_ext.py`
  - `get_captcha_status()` now reads explicit keys such as `show_captcha`, `captcha_required`, `need_captcha`, and returns `False` for an otherwise unknown dict.
- `src/platforms/kwork_listing.py`
  - Global SmartCaptcha script/token markers are no longer treated as a strong manual verification signal on normal HTTP 200 pages.
  - Script-only markers become meaningful only with challenge status/URL such as `403`, `429`, or `not_access.php`.
- `tests/unit/test_kwork_listing.py`
  - Added coverage for a normal page that includes global SmartCaptcha config/scripts.
- `tests/test_psr_features.py`
  - Added coverage for `show_captcha=false` and `show_captcha=true`.

Interpretation:

- The user's observation is consistent with the live data: clicking the captcha/verification session may open a normal logged-in Kwork session because `getCaptchaStatus` says `show_captcha=false`.
- A page containing `smartcaptcha.yandexcloud.net/captcha.js` is not by itself proof of a visible captcha. Kwork includes SmartCaptcha infrastructure globally.
- For buyer hunting, captcha status should be a health signal only. The scanner should not stop market analysis unless Kwork returns a real challenge URL/status or explicit `show_captcha=true`.

Verification:

- `python -m pytest C:\psr\tests\unit\test_kwork_listing.py::test_kwork_manual_verification_page_detects_smartcaptcha C:\psr\tests\unit\test_kwork_listing.py::test_kwork_manual_verification_ignores_global_smartcaptcha_script_on_normal_page C:\psr\tests\test_psr_features.py::TestKworkExtensions::test_get_captcha_status_reads_show_captcha_false C:\psr\tests\test_psr_features.py::TestKworkExtensions::test_get_captcha_status_reads_show_captcha_true -q` -> `4 passed`.
- `python -m pytest C:\psr\tests\unit\test_kwork_market.py C:\psr\tests\unit\test_kwork_routes.py C:\psr\tests\unit\test_kwork_listing.py -q` -> `57 passed`.
- `python -m ruff check C:\psr\src\platforms\kwork_ext.py C:\psr\src\platforms\kwork_listing.py C:\psr\tests\unit\test_kwork_listing.py` -> `All checks passed`.

Residual note:

- A broad run of `tests/test_psr_features.py` hit an unrelated `sqlite3.OperationalError: database is locked` in `TestMarkResponse.test_mark_response_records_audit`.
- Ruff on the whole legacy `tests/test_psr_features.py` still reports pre-existing unrelated issues such as unused imports and an old broad exception assertion. The new captcha tests themselves passed.
