import sys
sys.stdout.reconfigure(encoding='utf-8')
import asyncio
import os
import json
import pathlib
import mimetypes
import tempfile

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
from dotenv import load_dotenv
load_dotenv()

async def main():
    from src.browser.browser_manager import BrowserManager
    from src.platforms.kwork import KworkStateDataParser
    import httpx

    mgr = BrowserManager(headless=False)
    await mgr.init_auth("kwork.ru")

    # Step 1: Parse stateData from listing page
    list_url = "https://kwork.ru/projects?c=all&attr=211"
    page = await mgr.get_page(list_url)
    await page.sleep(4)
    html = await page.get_content()
    state = KworkStateDataParser.extract(html)

    if not state:
        print("stateData extraction failed")
        await mgr.stop()
        return

    wants = state.get("wants") or []
    if not wants:
        pag = state.get("pagination", {})
        if isinstance(pag, dict):
            wants = pag.get("data", [])
    print(f"Projects: {len(wants)}")

    # Find project with files
    target = None
    for w in wants:
        if not isinstance(w, dict):
            continue
        files = w.get("files") or []
        if isinstance(files, list) and len(files) > 0:
            target = w
            break

    if not target:
        print("No project with files found")
        await mgr.stop()
        return

    pid = target.get("id")
    ptitle = target.get("name") or target.get("title") or "?"
    files = target.get("files") or []
    print(f"Project #{pid}: {ptitle[:50]}")
    print(f"Files: {len(files)}")
    for f in files:
        print(f"  - {f.get('fname', '?')} url={f.get('url', '')[:80]}")

    # Step 2: Open INDIVIDUAL project page for screenshot
    project_url = f"https://kwork.ru/projects/{pid}"
    print(f"\nOpening individual page: {project_url}")
    proj_page = await mgr.get_page(project_url)
    await proj_page.sleep(4)

    # Scroll to top for clean screenshot
    await proj_page.evaluate("window.scrollTo(0, 0)")
    await proj_page.sleep(1)

    # Take full-page screenshot of the INDIVIDUAL project page
    screenshot_path = await mgr.take_screenshot(proj_page, str(pid), "01_project_page", full_page=True)
    print(f"Screenshot: {screenshot_path}")

    # Step 3: Get cookies for file download
    # Use CDP to get cookies directly from the page
    try:
        import nodriver as uc
        cookie_data = await proj_page.send(uc.cdp.network.get_cookies())
        cookies = {}
        for c in cookie_data:
            if "kwork.ru" in (c.domain or ""):
                cookies[c.name] = c.value
        print(f"Cookies for download: {len(cookies)}")
    except Exception as e:
        print(f"Cookie extraction error: {e}")
        cookies = {}

    # Step 4: Send to Telegram
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("ADMIN_CHAT_ID", "")
    if not token or not chat_id:
        print("No TELEGRAM_TOKEN/ADMIN_CHAT_ID")
        await mgr.stop()
        return

    print(f"\nSending to Telegram...")

    # 1. Send project info
    budget = target.get("priceLimit") or target.get("price") or 0
    offers = target.get("kwork_count") or target.get("offers") or 0
    desc = target.get("description") or ""
    # Strip HTML from description
    from bs4 import BeautifulSoup
    clean_desc = BeautifulSoup(desc, "lxml").text.strip()[:300] if desc else ""

    msg = (
        f"🔔 Проект #{pid}\n\n"
        f"📌 {ptitle}\n"
        f"💰 Бюджет: {budget} руб.\n"
        f"👥 Откликов: {offers}\n"
        f"📎 Файлов: {len(files)}\n\n"
        f"📝 Описание:\n{clean_desc}"
    )

    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg[:4000]},
        )
        print(f"  sendMessage: {resp.status_code}")

    # 2. Send screenshot of individual project page
    if screenshot_path:
        p = pathlib.Path(screenshot_path)
        if p.exists():
            async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                with p.open("rb") as f:
                    resp = await client.post(
                        f"https://api.telegram.org/bot{token}/sendPhoto",
                        data={"chat_id": chat_id, "caption": f"Скриншот проекта #{pid}"},
                        files={"photo": (p.name, f, "image/png")},
                    )
                print(f"  sendPhoto: {resp.status_code}")

    # 3. Download and send each file
    tmp_dir = pathlib.Path(tempfile.gettempdir()) / "psr_send"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    for fi, f in enumerate(files[:5]):
        fname = f.get("fname") or f.get("name") or f"file_{fi}"
        furl = f.get("url", "")
        if not furl:
            continue

        # Make URL absolute if relative
        if furl.startswith("/"):
            furl = f"https://kwork.ru{furl}"

        print(f"  Downloading {fname} from {furl[:80]}...")
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/139.0.0.0 Safari/537.36",
                "Referer": project_url,
                "Accept": "*/*",
            }

            async with httpx.AsyncClient(
                timeout=30,
                follow_redirects=True,
                trust_env=False,
                cookies=cookies,
                headers=headers,
            ) as client:
                resp = await client.get(furl)

                if resp.status_code == 200:
                    content = resp.content
                    print(f"    Downloaded {len(content)} bytes (content-type: {resp.headers.get('content-type', '?')})")

                    # Check if it's actually a file (not an HTML error page)
                    content_type = resp.headers.get("content-type", "")
                    if "text/html" in content_type:
                        print(f"    WARNING: Got HTML instead of file! Probably auth error.")
                        # Save for debugging
                        debug_path = tmp_dir / f"{fname}_debug.html"
                        debug_path.write_bytes(content)
                        print(f"    Saved debug HTML to {debug_path}")
                        continue

                    safe_name = "".join(c for c in fname if c.isalnum() or c in "._-() ") or f"file_{fi}"
                    fpath = tmp_dir / safe_name
                    fpath.write_bytes(content)

                    # Determine MIME from response headers first, then from filename
                    mime = content_type.split(";")[0].strip() if content_type else None
                    if not mime or mime == "application/octet-stream":
                        mime = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"

                    print(f"    Sending as {mime}...")

                    async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                        with fpath.open("rb") as ff:
                            resp = await client.post(
                                f"https://api.telegram.org/bot{token}/sendDocument",
                                data={"chat_id": chat_id, "caption": f"📎 {fname}"},
                                files={"document": (safe_name, ff, mime)},
                            )
                        print(f"    sendDocument: {resp.status_code}")
                        if resp.status_code != 200:
                            print(f"    Response: {resp.text[:200]}")
                else:
                    print(f"    Download failed: HTTP {resp.status_code}")
                    print(f"    Response: {resp.text[:200]}")
        except Exception as e:
            print(f"    Error: {e}")

    print("\nDone!")
    await mgr.close_page(proj_page)
    await mgr.stop()

asyncio.run(main())
