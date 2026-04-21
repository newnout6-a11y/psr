import asyncio, os
from kwork import Kwork
from dotenv import load_dotenv

load_dotenv()

async def test():
    api = Kwork(login=os.environ.get('KWORK_EMAIL'), password=os.environ.get('KWORK_PASSWORD'))
    try:
        p = await api.projects(limit=1)
        if p:
            proj = await api.project(project_id=p[0].id)
            print("Type of proj:", type(proj))
            print("Keys:", proj.keys() if isinstance(proj, dict) else dir(proj))
    finally:
        await api.close()

if __name__ == "__main__":
    asyncio.run(test())
