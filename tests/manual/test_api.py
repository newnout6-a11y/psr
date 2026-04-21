import asyncio
import os
from kwork import Kwork
from dotenv import load_dotenv

load_dotenv()

async def test():
    api = Kwork(login=os.getenv("KWORK_EMAIL"), password=os.getenv("KWORK_PASSWORD"))
    try:
        res = await api.get_projects(categories_ids=[11], page=1, price_from=500)
        print("get_projects success, count:", len(res))
    except Exception as e:
        print("get_projects failed:", e)

    try:
        res = await api.projects()
        print("projects() success")
    except Exception as e:
        print("projects() failed:", e)
    
    await api.close()

if __name__ == "__main__":
    asyncio.run(test())
