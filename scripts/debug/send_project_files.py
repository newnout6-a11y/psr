import sys
sys.stdout.reconfigure(encoding='utf-8')
import asyncio
import os
import json

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
from dotenv import load_dotenv
load_dotenv()

async def main():
    from src.platforms.kwork import get_kwork_service, KworkStateDataParser
    from src.browser.browser_manager import BrowserManager

    service = get_kwork_service()
    api = await service.get_api()
    if not api:
        print("API not available")
        return

    # Get projects via API
    projects, _ = await service.get_raw_projects(categories="all", page=1, query="python", price_from=500, price_to=10000)
    print(f"Got {len(projects)} projects from API")

    # Take first project with a URL
    if not projects:
        print("No projects")
        return

    p = projects[0]
    pid = p.get("id")
    title = p.get("name") or p.get("title") or "?"
    url = f"https://kwork.ru/projects/{pid}"
    print(f"Checking project #{pid}: {title[:50]}")
    print(f"URL: {url}")

    # Use browser to get stateData
    mgr = BrowserManager(headless=False)
    await mgr.init_auth("kwork.ru")
    page = await mgr.get_page(url)

    try:
        await page.sleep(3)
        html = await page.get_content()
        parser = KworkStateDataParser
        state = parser.extract(html)

        if state:
            wants = state.get("wants") or []
            if isinstance(wants, list) and wants:
                want = wants[0]
                files = want.get("files") or []
                print(f"\nstateData files: {len(files)}")
                for f in files[:5]:
                    fname = f.get("fname") or f.get("name") or "?"
                    furl = str(f.get("url", ""))[:100]
                    print(f"  - {fname} url={furl}")
                print(f"\ndate_create: {want.get('date_create', 'N/A')}")
                print(f"possiblePriceLimit: {want.get('possiblePriceLimit', 'N/A')}")
                print(f"allowHigherPrice: {want.get('allowHigherPrice', 'N/A')}")
                print(f"availableDurations: {want.get('availableDurations', 'N/A')}")

                # Now send to Telegram if we have files
                if files:
                    token = os.getenv("TELEGRAM_TOKEN", "")
                    chat_id = os.getenv("ADMIN_CHAT_ID", "")
                    if not token or not chat_id:
                        print("\nNo TELEGRAM_TOKEN/ADMIN_CHAT_ID — cannot send to Telegram")
                    else:
                        print(f"\nSending to Telegram chat {chat_id}...")
                        import httpx

                        # Send project info
                        msg = f"Проект #{pid}: {title[:60]}\nФайлов: {len(files)}"
                        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                            resp = await client.post(
                                f"https://api.telegram.org/bot{token}/sendMessage",
                                json={"chat_id": chat_id, "text": msg},
                            )
                            print(f"  sendMessage: {resp.status_code}")

                        # Send screenshot
                        screenshot_path = await mgr.take_screenshot(page, str(pid), "01_project_page", full_page=True)
                        if screenshot_path:
                            import pathlib
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

                        # Send each file
                        import tempfile
                        tmp_dir = pathlib.Path(tempfile.gettempdir()) / "psr_send"
                        tmp_dir.mkdir(parents=True, exist_ok=True)

                        for fi, f in enumerate(files[:5]):
                            fname = f.get("fname") or f.get("name") or f"file_{fi}"
                            furl = f.get("url", "")
                            if not furl:
                                continue
                            print(f"  Downloading {fname}...")
                            try:
                                # Get cookies from browser
                                cookies = await mgr.export_cookies("kwork.ru")
                                async with httpx.AsyncClient(timeout=30, follow_redirects=True, trust_env=False, cookies=cookies) as client:
                                    resp = await client.get(furl, headers={"Referer": url, "User-Agent": "Mozilla/5.0 Chrome/139.0.0.0"})
                                    if resp.status_code == 200:
                                        safe_name = "".join(c for c in fname if c.isalnum() or c in "._-") or f"file_{fi}"
                                        fpath = tmp_dir / safe_name
                                        fpath.write_bytes(resp.content)
                                        print(f"    Downloaded {len(resp.content)} bytes")

                                        import mimetypes
                                        mime = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
                                        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                                            with fpath.open("rb") as ff:
                                                resp = await client.post(
                                                    f"https://api.telegram.org/bot{token}/sendDocument",
                                                    data={"chat_id": chat_id, "caption": f"📎 {fname}"},
                                                    files={"document": (safe_name, ff, mime)},
                                                )
                                            print(f"    sendDocument: {resp.status_code}")
                                    else:
                                        print(f"    Download failed: HTTP {resp.status_code}")
                            except Exception as e:
                                print(f"    Error: {e}")
                else:
                    print("No files in this project")
            else:
                print("No wants in stateData")
        else:
            print("No stateData found")
    finally:
        await mgr.close_page(page)
        await mgr.stop()
        await service.close()

asyncio.run(main())
