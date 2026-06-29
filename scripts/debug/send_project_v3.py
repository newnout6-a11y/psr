import sys
sys.stdout.reconfigure(encoding='utf-8')
import asyncio
import os
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
    import nodriver as uc

    mgr = BrowserManager(headless=False)
    await mgr.init_auth("kwork.ru")

    # Parse listing for stateData
    page = await mgr.get_page("https://kwork.ru/projects?c=all&attr=211")
    await page.sleep(4)
    html = await page.get_content()
    state = KworkStateDataParser.extract(html)
    wants = state.get("wants") or state.get("pagination", {}).get("data", []) if state else []

    target = None
    for w in wants:
        if isinstance(w, dict) and isinstance(w.get("files"), list) and len(w.get("files", [])) > 0:
            target = w
            break

    if not target:
        print("No project with files")
        await mgr.stop()
        return

    pid = target["id"]
    ptitle = (target.get("name") or target.get("title") or "?")[:50]
    files = target.get("files") or []
    budget = target.get("priceLimit") or target.get("price") or 0
    offers = target.get("kwork_count") or target.get("offers") or 0

    print(f"Project #{pid}: {ptitle}")
    print(f"Files: {len(files)}")

    # Open INDIVIDUAL project page
    project_url = f"https://kwork.ru/projects/{pid}"
    proj_page = await mgr.get_page(project_url)
    await proj_page.sleep(4)
    await proj_page.evaluate("window.scrollTo(0, 0)")
    await proj_page.sleep(1)

    # Screenshot of individual page
    screenshot_path = await mgr.take_screenshot(proj_page, str(pid), "01_project_page", full_page=True)
    print(f"Screenshot: {screenshot_path}")

    # Get ALL cookies from browser via CDP
    all_cookies = await proj_page.send(uc.cdp.network.get_cookies())
    cookie_jar = {}
    for c in all_cookies:
        cookie_jar[c.name] = c.value
    print(f"Cookies: {len(cookie_jar)} names")

    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("ADMIN_CHAT_ID", "")
    if not token or not chat_id:
        print("No token/chat_id")
        await mgr.stop()
        return

    # 1. Text message
    from bs4 import BeautifulSoup
    desc_raw = target.get("description") or ""
    clean_desc = BeautifulSoup(desc_raw, "lxml").text.strip()[:300] if desc_raw else ""
    msg = f"🔔 Проект #{pid}\n\n📌 {ptitle}\n💰 Бюджет: {budget} руб.\n👥 Откликов: {offers}\n📎 Файлов: {len(files)}\n\n📝 {clean_desc}"

    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        r = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat_id, "text": msg[:4000]})
        print(f"sendMessage: {r.status_code}")

    # 2. Screenshot
    if screenshot_path and pathlib.Path(screenshot_path).exists():
        p = pathlib.Path(screenshot_path)
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            with p.open("rb") as f:
                r = await client.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id": chat_id, "caption": f"Скриншот проекта #{pid}"},
                    files={"photo": (p.name, f, "image/png")})
            print(f"sendPhoto: {r.status_code}")

    # 3. Files — download via browser fetch (same session, no auth issues)
    tmp_dir = pathlib.Path(tempfile.gettempdir()) / "psr_send"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    for fi, f in enumerate(files[:5]):
        fname = f.get("fname") or f.get("name") or f"file_{fi}"
        furl = f.get("url", "")
        if not furl:
            continue
        if furl.startswith("/"):
            furl = f"https://kwork.ru{furl}"

        print(f"Downloading {fname}...")

        # Use browser to download — fetch via page.evaluate returns base64
        try:
            js_code = f"""
            (async () => {{
                try {{
                    const resp = await fetch("{furl}", {{credentials: 'include'}});
                    if (!resp.ok) return {{error: 'HTTP ' + resp.status}};
                    const blob = await resp.blob();
                    const reader = new FileReader();
                    return new Promise(resolve => {{
                        reader.onload = () => resolve({{data: reader.result, type: blob.type, size: blob.size}});
                        reader.onerror = () => resolve({{error: 'read failed'}});
                        reader.readAsDataURL(blob);
                    }});
                }} catch(e) {{
                    return {{error: e.message}};
                }}
            }})()
            """
            result = await proj_page.evaluate(js_code)
            await asyncio.sleep(0.5)

            if not result or (isinstance(result, dict) and result.get("error")):
                print(f"  Browser fetch failed: {result}")
                # Fallback: httpx with cookies
                headers = {"User-Agent": "Mozilla/5.0 Chrome/139.0.0.0", "Referer": project_url}
                async with httpx.AsyncClient(timeout=60, follow_redirects=True, trust_env=False, cookies=cookie_jar, headers=headers) as client:
                    r = await client.get(furl)
                    if r.status_code == 200 and "text/html" not in r.headers.get("content-type", ""):
                        content = r.content
                        ct = r.headers.get("content-type", "application/octet-stream")
                    else:
                        print(f"  httpx fallback failed: {r.status_code}")
                        continue
            else:
                # Parse base64 data URL from browser fetch
                import base64
                data_url = result.get("data", "") if isinstance(result, dict) else ""
                if not data_url or not data_url.startswith("data:"):
                    print(f"  No data URL returned")
                    continue
                # data:type;base64,AAAA...
                header, b64data = data_url.split(",", 1)
                ct = header.split(":")[1].split(";")[0] if ":" in header else "application/octet-stream"
                content = base64.b64decode(b64data)

            print(f"  Got {len(content)} bytes, type={ct}")

            # Check not HTML error page
            if len(content) < 100 or (b"<html" in content[:200].lower()):
                print(f"  WARNING: too small or HTML — probably error page, skipping")
                continue

            safe_name = "".join(c for c in fname if c.isalnum() or c in "._-() ") or f"file_{fi}"
            fpath = tmp_dir / safe_name
            fpath.write_bytes(content)

            # Fix MIME for .docx
            if safe_name.endswith(".docx"):
                ct = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            elif safe_name.endswith(".pdf"):
                ct = "application/pdf"
            elif safe_name.endswith(".xlsx"):
                ct = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            elif safe_name.endswith(".png") or safe_name.endswith(".jpg") or safe_name.endswith(".jpeg"):
                ct = mimetypes.guess_type(safe_name)[0] or ct

            print(f"  Sending as {ct}...")
            async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
                with fpath.open("rb") as ff:
                    r = await client.post(f"https://api.telegram.org/bot{token}/sendDocument",
                        data={"chat_id": chat_id, "caption": f"📎 {fname}"},
                        files={"document": (safe_name, ff, ct)})
                print(f"  sendDocument: {r.status_code}")
                if r.status_code != 200:
                    print(f"  Error: {r.text[:200]}")

        except Exception as e:
            print(f"  Error: {e}")

    print("\nDone!")
    await mgr.close_page(proj_page)
    await mgr.stop()

asyncio.run(main())
