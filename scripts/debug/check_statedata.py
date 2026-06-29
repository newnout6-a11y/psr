import sys
sys.stdout.reconfigure(encoding='utf-8')
import asyncio
import os

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
from dotenv import load_dotenv
load_dotenv()

async def main():
    from src.browser.browser_manager import BrowserManager

    mgr = BrowserManager(headless=False)
    await mgr.init_auth("kwork.ru")

    # Try the projects listing page — stateData is on the listing
    url = "https://kwork.ru/projects?c=all&attr=211"
    page = await mgr.get_page(url)
    await page.sleep(3)
    html = await page.get_content()

    # Check for stateData
    if "window.stateData" in html:
        print("stateData FOUND on projects page!")
        # Extract a snippet
        idx = html.index("window.stateData")
        snippet = html[idx:idx+500]
        print(snippet[:300])
    else:
        print("No stateData on projects page")
        # Check what's in the HTML
        print(f"HTML length: {len(html)}")
        # Try individual project page
        print("\nTrying individual project page...")
        page2 = await mgr.get_page("https://kwork.ru/projects/3206389")
        await page2.sleep(3)
        html2 = await page2.get_content()
        if "window.stateData" in html2:
            print("stateData FOUND on project detail page!")
            idx2 = html2.index("window.stateData")
            snippet2 = html2[idx2:idx2+2000]
            print(snippet2[:500])

            # Parse with KworkStateDataParser
            from src.platforms.kwork import KworkStateDataParser
            state = KworkStateDataParser.extract(html2)
            if state:
                print(f"\nstateData parsed! keys: {list(state.keys())[:15]}")
                want_data = state.get("wantData")
                if want_data:
                    print(f"wantData keys: {list(want_data.keys())[:15]}")
                    files = want_data.get("files") or []
                    print(f"Files: {len(files)}")
                    for f in files[:3]:
                        print(f"  - {f.get('fname', '?')} url={str(f.get('url', ''))[:80]}")
                wants = state.get("wants") or []
                print(f"wants: {len(wants)}")
            else:
                print("stateData extraction failed")
        else:
            print("No stateData on project detail page either")
            print(f"HTML2 length: {len(html2)}")

    await mgr.close_page(page)
    await mgr.stop()

asyncio.run(main())
