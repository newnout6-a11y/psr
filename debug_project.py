import asyncio
import os
import nodriver as uc
from dotenv import load_dotenv

load_dotenv()

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
    path = r"c:\psr\data\kwork_project_check.png"
    await page.save_screenshot(path)
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
