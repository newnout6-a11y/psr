import sys
sys.stdout.reconfigure(encoding='utf-8')
import asyncio
import os
import json

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
from dotenv import load_dotenv
load_dotenv()

async def main():
    from src.platforms.kwork import get_kwork_service
    from src.platforms.kwork_ext import KworkExtensions

    service = get_kwork_service()
    api = await service.get_api()
    if not api:
        print("API not available")
        return

    # Get projects first
    projects, meta = await KworkExtensions.get_raw_projects(
        api, categories="all", page=1, query="python", price_from=500, price_to=10000,
    )

    print(f"Got {len(projects)} projects from /projects")
    print()

    # Now get project details for first 3
    for p in projects[:3]:
        pid = p.get("id")
        title = p.get("name") or p.get("title") or "?"
        print(f"=== Project #{pid}: {title[:50]} ===")

        details = await KworkExtensions.get_project_details(api, pid)
        if details:
            print(f"  Keys: {list(details.keys())[:20]}")
            files = details.get("files") or []
            print(f"  files: {len(files) if isinstance(files, list) else type(files)}")
            if isinstance(files, list):
                for f in files[:3]:
                    fname = f.get("fname") or f.get("name") or "?"
                    url = str(f.get("url", ""))[:100]
                    print(f"    - {fname} url={url}")
            print(f"  date_create: {details.get('date_create', 'N/A')}")
            print(f"  possible_price_limit: {details.get('possiblePriceLimit', 'N/A')}")
            skills = details.get("skills") or details.get("skills_possible") or []
            print(f"  skills: {skills[:5]}")
        else:
            print("  No details returned")
        print()

    await service.close()

asyncio.run(main())
