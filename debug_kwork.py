"""Дебаг: сохраняем HTML и скриншот того что видит Nodriver на Kwork."""
import asyncio
import os
import nodriver as uc
from dotenv import load_dotenv

load_dotenv()

async def main():
    browser = await uc.start(headless=True)
    page = await browser.get("about:blank")
    
    # Куки
    remember = os.getenv("KWORK_COOKIE_REMEMBERME", "")
    user_id = os.getenv("KWORK_COOKIE_USERID", "")
    if remember:
        await page.send(uc.cdp.network.set_cookie(
            name="slrememberme", value=remember,
            domain="kwork.ru", path="/"
        ))
        await page.send(uc.cdp.network.set_cookie(
            name="userId", value=user_id,
            domain="kwork.ru", path="/"
        ))
    
    url = "https://kwork.ru/projects?c=all&attr=211"
    print(f"Загружаем: {url}")
    await page.get(url)
    await asyncio.sleep(6)
    
    # Скролл
    await page.evaluate("window.scrollTo(0, 500)")
    await asyncio.sleep(3)
    
    html = await page.get_content()
    
    # Сохраняем HTML
    with open(r"c:\psr\data\kwork_debug.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML сохранён ({len(html)} символов)")
    
    # Ищем карточки
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    
    # Пробуем разные селекторы
    for selector in ["div.want-card", "div.wants-card", "div[class*=want]", "div[class*=project]", "a[href*=projects]"]:
        found = soup.select(selector)
        print(f"  {selector}: {len(found)} элементов")
    
    # Скриншот
    screenshot_path = r"c:\psr\data\kwork_debug.png"
    await page.save_screenshot(screenshot_path)
    print(f"Скриншот: {screenshot_path}")
    
    browser.stop()

if __name__ == "__main__":
    asyncio.run(main())
