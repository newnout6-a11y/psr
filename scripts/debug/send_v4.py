import sys, asyncio, os, pathlib, mimetypes, tempfile, base64
sys.stdout.reconfigure(encoding='utf-8')
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
from dotenv import load_dotenv
load_dotenv()

async def main():
    from src.browser.browser_manager import BrowserManager
    from src.platforms.kwork import KworkStateDataParser
    import httpx, nodriver as uc

    mgr = BrowserManager(headless=False)
    await mgr.init_auth("kwork.ru")

    # 1. Parse listing
    page = await mgr.get_page("https://kwork.ru/projects?c=all&attr=211")
    await page.sleep(4)
    state = KworkStateDataParser.extract(await page.get_content())
    wants = state.get("wants") or (state.get("pagination",{}).get("data") if state else []) or []

    target = next((w for w in wants if isinstance(w,dict) and isinstance(w.get("files"),list) and len(w.get("files",[]))>0), None)
    if not target:
        print("No project with files"); await mgr.stop(); return

    pid = target["id"]
    ptitle = (target.get("name") or "?")[:50]
    files = target.get("files") or []
    budget = target.get("priceLimit") or target.get("price") or 0
    offers = target.get("kwork_count") or 0
    print(f"#{pid} {ptitle} files={len(files)}")

    # 2. Open individual project page + screenshot
    purl = f"https://kwork.ru/projects/{pid}"
    pp = await mgr.get_page(purl)
    await pp.sleep(4)
    await pp.evaluate("window.scrollTo(0,0)")
    await pp.sleep(1)
    ss = await mgr.take_screenshot(pp, str(pid), "01_project_page", full_page=True)
    print(f"Screenshot: {ss}")

    # 3. Get cookies via CDP from the page
    raw_cookies = await pp.send(uc.cdp.network.get_cookies())
    cookies = {c.name: c.value for c in raw_cookies}
    print(f"Cookies: {len(cookies)}")

    # 4. Send to TG
    token = os.getenv("TELEGRAM_TOKEN","")
    cid = os.getenv("ADMIN_CHAT_ID","")
    if not token or not cid:
        print("No token"); await mgr.stop(); return

    from bs4 import BeautifulSoup
    desc = BeautifulSoup(target.get("description",""), "lxml").text.strip()[:300]
    msg = f"🔔 #{pid}\n\n📌 {ptitle}\n💰 {budget}₽\n👥 {offers} откликов\n📎 {len(files)} файл(ов)\n\n{desc}"

    async with httpx.AsyncClient(timeout=30, trust_env=False) as c:
        await c.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id":cid,"text":msg[:4000]})
        print("sendMessage: ok")

    if ss and pathlib.Path(ss).exists():
        p = pathlib.Path(ss)
        async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
            with p.open("rb") as f:
                r = await c.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id":cid,"caption":f"Проект #{pid}"},
                    files={"photo":(p.name,f,"image/png")})
            print(f"sendPhoto: {r.status_code}")

    # 5. Download + send files via httpx with cookies (NOT browser fetch)
    tmp = pathlib.Path(tempfile.gettempdir())/"psr_send"
    tmp.mkdir(parents=True, exist_ok=True)

    for fi, f in enumerate(files[:5]):
        fname = f.get("fname") or f.get("name") or f"file_{fi}"
        furl = f.get("url","")
        if furl.startswith("/"): furl = f"https://kwork.ru{furl}"
        if not furl: continue

        print(f"DL {fname}...")
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True, trust_env=False,
                                         cookies=cookies,
                                         headers={"User-Agent":"Mozilla/5.0 Chrome/139.0.0.0",
                                                  "Referer":purl}) as c:
                r = await c.get(furl)
                if r.status_code != 200:
                    print(f"  HTTP {r.status_code}"); continue
                ct = r.headers.get("content-type","")
                if "text/html" in ct:
                    print(f"  Got HTML (auth fail)"); continue
                content = r.content
                print(f"  {len(content)} bytes type={ct}")

            safe = "".join(ch for ch in fname if ch.isalnum() or ch in "._-() ") or f"f{fi}"
            fpath = tmp / safe
            fpath.write_bytes(content)

            # Fix MIME
            mime_map = {".docx":"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        ".xlsx":"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        ".pdf":"application/pdf", ".png":"image/png", ".jpg":"image/jpeg",
                        ".zip":"application/zip", ".rar":"application/vnd.rar"}
            mime = mime_map.get(pathlib.Path(safe).suffix.lower(), ct or "application/octet-stream")

            async with httpx.AsyncClient(timeout=60, trust_env=False) as c:
                with fpath.open("rb") as ff:
                    r = await c.post(f"https://api.telegram.org/bot{token}/sendDocument",
                        data={"chat_id":cid,"caption":f"📎 {fname}"},
                        files={"document":(safe,ff,mime)})
                print(f"  sendDocument: {r.status_code}")
        except Exception as e:
            print(f"  ERR: {e}")

    print("DONE")
    await mgr.close_page(pp)
    await mgr.stop()

asyncio.run(main())
