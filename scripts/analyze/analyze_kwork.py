import sys
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.paths import KWORK_DEBUG_HTML_FILE

with KWORK_DEBUG_HTML_FILE.open("r", encoding="utf-8") as f:
    soup = BeautifulSoup(f.read(), "lxml")

cards = soup.select("div.want-card")
print(f"Карточек: {len(cards)}\n")

if cards:
    card = cards[0]
    # Найдём ссылку на проект
    a = card.find("a", href=lambda h: h and "/projects/" in h)
    if a:
        print(f"Ссылка: {a.get('href')}")
        print(f"Заголовок: {a.text.strip()[:80]}")
    
    # Поищем цену
    for el in card.find_all(["div", "span"]):
        text = el.text.strip()
        if "₽" in text or "руб" in text.lower() or "до" in text.lower():
            cls = " ".join(el.get("class", []))
            print(f"Цена? <{el.name} class='{cls}'> {text[:60]}")
    
    # Описание
    for el in card.find_all("div"):
        cls = " ".join(el.get("class", []))
        text = el.text.strip()
        if len(text) > 50 and "description" in cls.lower() or "text" in cls.lower():
            print(f"Описание? <div class='{cls}'> {text[:100]}")
    
    print("\n--- Все классы внутри карточки ---")
    for el in card.find_all(True):
        cls = " ".join(el.get("class", []))
        if cls:
            print(f"  <{el.name}> .{cls}")
