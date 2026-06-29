import sys, asyncio, os, pathlib, mimetypes, tempfile
sys.stdout.reconfigure(encoding='utf-8')
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
from dotenv import load_dotenv
load_dotenv()

async def main():
    from src.browser.browser_manager import BrowserManager
    from src.platforms.kwork import KworkStateDataParser
    import httpx

    mgr = BrowserManager(headless=False)
    await mgr.init_auth("kwork.ru")

    # 1. Listing → stateData → find project with files
    page = await mgr.get_page("https://kwork.ru/projects?c=all&attr=211")
    await page.sleep(4)
    state = KworkStateDataParser.extract(await page.get_content())
    wants = state.get("wants") or (state.get("pagination",{}).get("data") if state else []) or []
    target = next((w for w in wants if isinstance(w,dict) and isinstance(w.get("files"),list) and len(w.get("files",[]))>0), None)
    if not target:
        print("No files found"); await mgr.stop(); return

    pid = target["id"]
    ptitle = (target.get("name") or "?")[:50]
    files = target.get("files") or []
    print(f"#{pid} {ptitle} files={len(files)}")

    # 2. Open individual project page for screenshot
    purl = f"https://kwork.ru/projects/{pid}"
    pp = await mgr.get_page(purl)
    await pp.sleep(4)
    await pp.evaluate("window.scrollTo(0,0)")
    await pp.sleep(1)
    ss = await mgr.take_screenshot(pp, str(pid), "01_project_page", full_page=True)
    print(f"Screenshot done")

    # 3. Get cookies from Session Hub (NOT CDP — that hangs)
    hub_url = os.getenv("SESSION_HUB_URL", "http://127.0.0.1:8669/cookies")
    cookies = {}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{hub_url}?domain=kwork.ru")
            if r.status_code == 200:
                data = r.json()
                for ck in data.get("cookies", []):
                    cookies[ck.get("name","")] = ck.get("value","")
    except Exception:
        pass
    print(f"Cookies from Hub: {len(cookies)}")

    # 4. Send to TG
    token = os.getenv("TELEGRAM_TOKEN","")
    cid = os.getenv("ADMIN_CHAT_ID","")
    if not token or not cid:
        print("No TG creds"); await mgr.stop(); return

    from bs4 import BeautifulSoup
    desc = BeautifulSoup(target.get("description",""), "lxml").text.strip()[:300]
    msg = f"🔔 #{pid}\n📌 {ptitle}\n💰 {target.get('priceLimit',0)}₽\n👥 {target.get('kwork_count',0)} откликов\n📎 {len(files)} файл(ов)\n\n{desc}"
    async with httpx.AsyncClient(timeout=30, trust_env=False) as c:
        await c.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id":cid,"text":msg[:4000]})
        print("msg sent")

    if ss and pathlib.Path(ss).exists():
        p = pathlib.Path(ss)
        async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
            with p.open("rb") as f:
                await c.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id":cid,"caption":f"Проект #{pid}"},
                    files={"photo":(p.name,f,"image/png")})
        print("photo sent")

    # 5. Download + send files
    tmp = pathlib.Path(tempfile.gettempdir())/"psr_send"; tmp.mkdir(parents=True, exist_ok=True)
    for fi, f in enumerate(files[:5]):
        fname = f.get("fname") or f.get("name") or f"f{fi}"
        furl = f.get("url","")
        if furl.startswith("/"): furl = f"https://kwork.ru{furl}"
        if not furl: continue
        print(f"DL {fname}...")
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True, trust_env=False,
                                         cookies=cookies,
                                         headers={"User-Agent":"Mozilla/5.0 Chrome/139.0.0.0","Referer":purl}) as c:
                r = await c.get(furl)
                if r.status_code != 200: print(f"  HTTP {r.status_code}"); continue
                ct = r.headers.get("content-type","")
                if "text/html" in ct: print("  HTML (auth fail)"); continue
                content = r.content
                print(f"  {len(content)} bytes")

            safe = "".join(ch for ch in fname if ch.isalnum() or ch in "._-() ") or f"f{fi}"
            (tmp / safe).write_bytes(content)
            mime_map = {".docx":"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        ".xlsx":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        ".pdf":"application/pdf"}
            mime = mime_map.get(pathlib.Path(safe).suffix.lower(), ct or "application/octet-stream")
            async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
                with (tmp/safe).open("rb") as ff:
                    r = await c.post(f"https://api.telegram.org/bot{token}/sendDocument",
                        data={"chat_id":cid,"caption":f"📎 {fname}"},
                        files={"document":(safe,ff,mime)})
                print(f"  doc sent: {r.status_code}")
        except Exception as e:
            print(f"  ERR: {e}")

    print("DONE")
    await mgr.close_page(pp)
    await mgr.stop()

asyncio.run(main())
