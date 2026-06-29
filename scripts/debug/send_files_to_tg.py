import sys
sys.stdout.reconfigure(encoding='utf-8')
import asyncio
import os
import json

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
from dotenv import load_dotenv
load_dotenv()

async def main():
    from src.browser.browser_manager import BrowserManager
    from src.platforms.kwork import KworkStateDataParser
    import httpx
    import pathlib

    mgr = BrowserManager(headless=False)
    await mgr.init_auth("kwork.ru")

    url = "https://kwork.ru/projects?c=all&attr=211"
    page = await mgr.get_page(url)
    await page.sleep(4)
    html = await page.get_content()

    state = KworkStateDataParser.extract(html)
    if not state:
        print("stateData extraction failed")
        await mgr.stop()
        return

    print(f"stateData keys: {list(state.keys())[:20]}")

    # Find projects in stateData
    wants = state.get("wants") or []
    pagination_data = state.get("pagination", {}).get("data") if isinstance(state.get("pagination"), dict) else None
    if not wants and pagination_data:
        wants = pagination_data
        print("Using pagination.data as wants")

    print(f"Projects found: {len(wants)}")

    # Find a project with files
    project_with_files = None
    for w in wants:
        if not isinstance(w, dict):
            continue
        files = w.get("files") or []
        if isinstance(files, list) and len(files) > 0:
            project_with_files = w
            break

    if not project_with_files:
        print("No projects with files found in stateData")
        # Show first 3 projects with their fields
        for w in wants[:3]:
            if isinstance(w, dict):
                wid = w.get("id", "?")
                wtitle = w.get("name") or w.get("title") or "?"
                files = w.get("files") or []
                print(f"  #{wid} {wtitle[:40]} files={len(files) if isinstance(files, list) else type(files)}")
        await mgr.stop()
        return

    pid = project_with_files.get("id")
    ptitle = project_with_files.get("name") or project_with_files.get("title") or "?"
    files = project_with_files.get("files") or []
    print(f"\nFound project with files!")
    print(f"  #{pid} {ptitle[:50]}")
    print(f"  Files: {len(files)}")
    for f in files[:5]:
        print(f"    - {f.get('fname', '?')} url={str(f.get('url', ''))[:80]}")

    # Take screenshot
    screenshot_path = await mgr.take_screenshot(page, str(pid), "01_project_page", full_page=True)
    print(f"  Screenshot: {screenshot_path}")

    # Send to Telegram
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("ADMIN_CHAT_ID", "")
    if not token or not chat_id:
        print("No TELEGRAM_TOKEN/ADMIN_CHAT_ID")
        await mgr.stop()
        return

    print(f"\nSending to Telegram chat {chat_id}...")

    # 1. Send project info message
    msg = f"Проект #{pid}: {ptitle[:60]}\nФайлов: {len(files)}"
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
        )
        print(f"  sendMessage: {resp.status_code}")

    # 2. Send screenshot
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

    # 3. Send each file
    cookies = await mgr.export_cookies("kwork.ru")
    import tempfile
    import mimetypes
    tmp_dir = pathlib.Path(tempfile.gettempdir()) / "psr_send"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    for fi, f in enumerate(files[:5]):
        fname = f.get("fname") or f.get("name") or f"file_{fi}"
        furl = f.get("url", "")
        if not furl:
            continue
        print(f"  Downloading {fname}...")
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, trust_env=False, cookies=cookies) as client:
                resp = await client.get(furl, headers={"Referer": url, "User-Agent": "Mozilla/5.0 Chrome/139.0.0.0"})
                if resp.status_code == 200:
                    safe_name = "".join(c for c in fname if c.isalnum() or c in "._-") or f"file_{fi}"
                    fpath = tmp_dir / safe_name
                    fpath.write_bytes(resp.content)
                    print(f"    Downloaded {len(resp.content)} bytes")
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

    print("\nDone!")
    await mgr.close_page(page)
    await mgr.stop()

asyncio.run(main())
