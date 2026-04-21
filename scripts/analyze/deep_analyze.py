import asyncio
import os
import sys
from pathlib import Path
import nodriver as uc
from dotenv import load_dotenv
from bs4 import BeautifulSoup

load_dotenv()
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.paths import PAGE_STRUCTURE_FILE, KWORK_FULL_PAGE_PNG_FILE, ensure_parent

async def main():
    print("Запускаем глубокий анализ страницы...")
    browser = await uc.start(headless=True)
    page = await browser.get("about:blank")
    
    # Куки
    remember = os.getenv("KWORK_COOKIE_REMEMBERME", "")
    user_id = os.getenv("KWORK_COOKIE_USERID", "")
    await page.send(uc.cdp.network.set_cookie(
        name="slrememberme", value=remember, domain="kwork.ru", path="/"
    ))
    await page.send(uc.cdp.network.set_cookie(
        name="userId", value=user_id, domain="kwork.ru", path="/"
    ))
    
    url = "https://kwork.ru/projects/3148339"
    await page.get(url)
    await asyncio.sleep(6)
    
    # Скроллим вниз несколько раз, чтобы всё прогрузилось
    for i in range(3):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(2)
    
    # Делаем скриншот всей страницы (пытаемся)
    await page.save_screenshot(str(ensure_parent(KWORK_FULL_PAGE_PNG_FILE)))
    
    # Достаём HTML
    html = await page.get_content()
    soup = BeautifulSoup(html, "lxml")
    
    with ensure_parent(PAGE_STRUCTURE_FILE).open("w", encoding="utf-8") as f:
        f.write("=== КНОПКИ ===\n")
        for btn in soup.find_all("button"):
            f.write(f"Текст: '{btn.text.strip()}' | Классы: {btn.get('class')}\n")
            
        f.write("\n=== ССЫЛКИ С ТЕКСТОМ ===\n")
        for a in soup.find_all("a"):
            txt = a.text.strip()
            if txt:
                f.write(f"Текст: '{txt}' | Href: {a.get('href')}\n")
                
        f.write("\n=== ПОЛЯ ВВОДА (FORMS) ===\n")
        for t in soup.find_all(["textarea", "input"]):
            f.write(f"Тип: {t.name} | Name: {t.get('name')} | ID: {t.get('id')}\n")

    print("Анализ завершен. Файлы в data/debug/")
    browser.stop()

if __name__ == "__main__":
    asyncio.run(main())
