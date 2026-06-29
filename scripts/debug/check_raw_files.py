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

    # Get raw projects with all fields
    projects, meta = await KworkExtensions.get_raw_projects(
        api, categories="all", page=1, query="python", price_from=500, price_to=10000,
    )

    print(f"Got {len(projects)} projects")
    for p in projects[:5]:
        pid = p.get("id")
        title = p.get("name") or p.get("title") or "?"
        files = p.get("files") or []
        print(f"\n  #{pid} {title[:50]}")
        print(f"  files count: {len(files) if isinstance(files, list) else 'not list'}")
        if isinstance(files, list):
            for f in files[:3]:
                fname = f.get("fname") or f.get("name") or "?"
                url = str(f.get("url", ""))[:80]
                print(f"    - {fname} url={url}")
        # Check other useful fields
        print(f"  date_create: {p.get('date_create', 'N/A')}")
        print(f"  possible_price_limit: {p.get('possiblePriceLimit', 'N/A')}")
        print(f"  allow_higher_price: {p.get('allowHigherPrice', 'N/A')}")
        print(f"  already_work: {p.get('alreadyWork', 'N/A')}")
        skills = p.get("skills") or p.get("skills_possible") or []
        print(f"  skills: {skills[:3] if isinstance(skills, list) else 'N/A'}")

    await service.close()

asyncio.run(main())
