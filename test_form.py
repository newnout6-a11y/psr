import asyncio
import os
import nodriver as uc
from dotenv import load_dotenv
from bs4 import BeautifulSoup

load_dotenv()

async def main():
    print("Проверяем форму отклика...")
    browser = await uc.start(headless=True)
    page = await browser.get("about:blank")
    
    # Куки
    cookies = {
        "slrememberme": os.getenv("KWORK_COOKIE_REMEMBERME", ""),
        "userId": os.getenv("KWORK_COOKIE_USERID", ""),
    }
    await page.send(uc.cdp.network.set_cookie(
        name="slrememberme", value=cookies["slrememberme"], domain="kwork.ru", path="/"
    ))
    await page.send(uc.cdp.network.set_cookie(
        name="userId", value=cookies["userId"], domain="kwork.ru", path="/"
    ))
    
    url = "https://kwork.ru/projects/3148339"
    await page.get(url)
    await asyncio.sleep(5)
    
    # 1. Жмём "Предложить услугу"
    btn = await page.find("Предложить услугу", timeout=5)
    if btn:
        print("Нажимаем кнопку 'Предложить услугу'...")
        await btn.click()
        await asyncio.sleep(3)
        
        # 2. Делаем скриншот формы
        await page.save_screenshot(r"c:\psr\data\kwork_offer_form.png")
        print("Скриншот формы сохранен.")
        
        # 3. Анализируем поля формы
        html = await page.get_content()
        soup = BeautifulSoup(html, "lxml")
        
        with open(r"c:\psr\data\form_structure.txt", "w", encoding="utf-8") as f:
            f.write("=== ПОЛЯ ФОРМЫ ПОСЛЕ КЛИКА ===\n")
            for t in soup.find_all(["textarea", "input", "button"]):
                txt = t.text.strip()
                f.write(f"Тег: {t.name} | Текст: '{txt}' | ID: {t.get('id')} | Name: {t.get('name')} | Type: {t.get('type')}\n")
    else:
        print("Кнопка не найдена!")

    browser.stop()

if __name__ == "__main__":
    asyncio.run(main())
