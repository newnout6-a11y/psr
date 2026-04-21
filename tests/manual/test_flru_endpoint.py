"""
Проверка страницы проекта Kwork — видимый режим
"""
import asyncio
from playwright.async_api import async_playwright

async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    context = await browser.new_context()
    await context.add_cookies([
        {"name": "slrememberme", "value": "20354236_%242y%2410%249l4K9AzuB%2F%2F981sWRIzTTOtiL3qXE4Z.B%2FVmBLhT3wzOtGcFAMZLm", "domain": ".kwork.ru", "path": "/"},
        {"name": "userId", "value": "20354236", "domain": ".kwork.ru", "path": "/"},
    ])
    page = await context.new_page()

    await page.goto("https://kwork.ru/projects/3142466", wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(5000)

    await page.screenshot(path="debug_kwork_project.png", full_page=True)

    buttons = await page.query_selector_all('button, a[class*="btn"], a[class*="button"]')
    print("=== Кнопки на странице проекта ===")
    for btn in buttons:
        text = (await btn.inner_text()).strip()
        if text:
            print(f"  {text[:80]}")

    # Все элементы с текстом отклик/предложение
    all_els = await page.query_selector_all('*')
    for el in all_els:
        text = (await el.inner_text()).strip()
        if any(kw in text.lower() for kw in ['отклик', 'предложен', 'отозв', 'bid', 'offer']):
            tag = await el.evaluate("el => el.tagName")
            print(f"  <{tag}>: {text[:80]}")

    await browser.close()
    await pw.stop()

asyncio.run(main())
