# Kwork Proxy Rotation Notes

Date: 2026-07-08 18:42 MSK

## What PSR already has

- External proxy data path: `http://127.0.0.1:17990`.
- VPNTE control API: `http://127.0.0.1:17873` by default, or the URL from `%APPDATA%\VPN Tunnel Enforcer\external-proxy-control-endpoint.json`.
- Control token source: `VPNTE_CONTROL_TOKEN` env var or `%APPDATA%\VPN Tunnel Enforcer\external-proxy-control-token`.
- Do not print or store the control token.

## Code entry points

- `C:\psr\src\utils\vpnte_proxy.py`
  - `VpnteProxyClient().status()` reads `/status`.
  - `VpnteProxyClient().start()` sends `POST /start`.
  - `VpnteProxyClient().rotate()` sends `POST /rotate`.
  - `kwork_http_proxy_url(rotate=False)` returns the proxy PSR should use for Kwork traffic.
  - `kwork_http_proxy_url(rotate=True)` rotates the backing VPN profile and returns the same local proxy URL.
- `C:\psr\src\api\routes\settings.py`
  - `GET /api/settings/network/status`
  - `POST /api/settings/network/vpnte/start`
  - `POST /api/settings/network/vpnte/rotate`

## Runtime env knobs

- `VPNTE_PROXY_ENABLED=true` enables the helper.
- `VPNTE_PROXY_ROTATE_ON_NEXT=true` means callers may rotate before the next Kwork-facing batch.
- `VPNTE_PROXY_STRICT=true` fails instead of silently falling back when VPNTE is unavailable.
- `VPNTE_PROXY_COUNTRY` can constrain profile selection by country.
- `VPNTE_PROXY_PROFILE_ID` can force a specific profile.
- `VPNTE_PROXY_PORT=17990` keeps the exposed local proxy stable.
- `VPNTE_PROXY_CACHE_TTL=60` avoids calling the control API on every request.

## Live validation

One rotation was tested successfully on 2026-07-08 18:42 MSK.

- Before: running profile `polandvless3`.
- Rotate: `POST /rotate` returned success.
- After: running profile `norwayvless1`.
- Local proxy URL stayed on port `17990`.
- A small external sanity-check request through the proxy succeeded.
- IP-like values were redacted from terminal output and are not stored here.

Second rotation validation on 2026-07-08 19:45 MSK:

- Config source: control URL from `%APPDATA%\VPN Tunnel Enforcer\external-proxy-control-endpoint.json`; token from `%APPDATA%\VPN Tunnel Enforcer\external-proxy-control-token`.
- Before: running profile `russia1`.
- Rotate: `POST /rotate` returned success.
- After: running profile `russia3`.
- Local proxy URL stayed `http://127.0.0.1:17990`.
- `kwork_http_proxy_url(rotate=False)` returned `http://127.0.0.1:17990`.
- A lightweight HTTPS sanity-check through the proxy succeeded with HTTP 200.
- No token, cookie, or external IP value is stored in this note.

## Operational rule for Kwork API research

Use the fast API path first. Rotate the VPNTE profile only between small batches or after a protection/block response. Do not rotate inside tight per-item loops. After rotation, validate with `/status` and one lightweight proxy request before continuing larger Kwork probes.
