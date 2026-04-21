import asyncio
import os
import sys
from pathlib import Path
import nodriver as uc
from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.paths import KWORK_PROJECT_CHECK_PNG_FILE, ensure_parent

async def main():
    print("Запускаем дебаг Kwork с куками...")
    browser = await uc.start(headless=True)
    page = await browser.get("about:blank")
    
    # Ставим твои куки
    remember = os.getenv("KWORK_COOKIE_REMEMBERME", "")
    user_id = os.getenv("KWORK_COOKIE_USERID", "")
    
    await page.send(uc.cdp.network.set_cookie(
        name="slrememberme", value=remember,
        domain="kwork.ru", path="/"
    ))
    await page.send(uc.cdp.network.set_cookie(
        name="userId", value=user_id,
        domain="kwork.ru", path="/"
    ))
    
    # Идём на конкретный проект
    url = "https://kwork.ru/projects/3148339"
    print(f"Переходим на {url}")
    await page.get(url)
    await asyncio.sleep(5) # Ждём прогрузки
    
    # Делаем скриншот
    path = ensure_parent(KWORK_PROJECT_CHECK_PNG_FILE)
    await page.save_screenshot(str(path))
    print(f"Скриншот сохранен: {path}")
    
    # Сохраним текст всех кнопок на странице
    buttons = await page.find_all("button")
    print("Найденные кнопки:")
    for btn in buttons:
        print(f"  - {btn.text.strip()}")
        
    # И ссылки
    links = await page.find_all("a")
    print("Подозрительные ссылки:")
    for link in links:
        txt = link.text.strip()
        if "отклик" in txt.lower() or "предложить" in txt.lower():
            print(f"  - {txt}")

    browser.stop()

if __name__ == "__main__":
    asyncio.run(main())
