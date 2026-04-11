import asyncio
import os
import nodriver as uc
from dotenv import load_dotenv

load_dotenv()

async def main():
    print("Запускаем Nodriver...")
    browser = await uc.start(headless=True)
    page = await browser.get("about:blank")
    
    cookies = {
        "slrememberme": os.getenv("KWORK_COOKIE_REMEMBERME", ""),
        "userId": os.getenv("KWORK_COOKIE_USERID", ""),
    }
    
    print(f"Куки из .env: slrememberme={cookies['slrememberme'][:10]}... userId={cookies['userId']}")
    
    await page.get("https://kwork.ru/")
    for k, v in cookies.items():
        await page.send(uc.cdp.network.set_cookie(
            name=k,
            value=str(v),
            domain="kwork.ru",
            path="/"
        ))
        
    print("Куки установлены. Переходим на страницу биржи: https://kwork.ru/projects")
    await page.get("https://kwork.ru/projects")
    await asyncio.sleep(5)
    
    # Сохраняем скриншот как доказательство!
    screenshot_path = r"C:\Users\Redmi\.gemini\antigravity\brain\dd90bc6a-77cd-42f8-b95c-23dc17b44d16\kwork_proof.png"
    await page.save_screenshot(screenshot_path)
    
    print(f"Скриншот сохранен по пути: {screenshot_path}")
    
    # Проверим, авторизованы ли мы
    html = await page.get_content()
    if 'class="login-js"' in html or 'Войти' in html:
        print("[ОШИБКА] Куки невалидные. Kwork требует логин.")
    else:
        print("[УСПЕХ] Аккаунт авторизован! Биржа доступна для откликов.")
        
    browser.stop()

if __name__ == "__main__":
    asyncio.run(main())
