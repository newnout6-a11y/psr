import asyncio, os
from kwork import Kwork
from dotenv import load_dotenv

load_dotenv()

async def test():
    api = Kwork(login=os.environ.get('KWORK_EMAIL'), password=os.environ.get('KWORK_PASSWORD'))
    try:
        p = await api.get_projects(categories_ids=[11], page=1, price_from=500)
        print("Type in get_projects:", type(p[0]))
        d = await api.project(project_id=p[0].id)
        print("Type from project():", type(d))
        print("Has keys? ", hasattr(d, 'keys'))
    finally:
        await api.close()

if __name__ == "__main__":
    asyncio.run(test())
