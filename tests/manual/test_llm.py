import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.action.proposal_generator import ProposalGenerator
from src.parsers.base_parser import ProjectItem
from dotenv import load_dotenv

load_dotenv()

async def main():
    generator = ProposalGenerator()
    
    # Фейковый, но реалистичный проект "тг бот"
    project = ProjectItem(
        id="999999",
        title="Нужен Telegram бот для автосервиса",
        description="Требуется разработать тг бота для записи клиентов на шиномонтаж. Должна быть админка, чтобы механики видели расписание. Писать строго на Python (aiogram 3). База PostgreSQL. Сроки горят.",
        budget=12000.0,
        currency="RUB",
        skills=["Python", "Telegram API", "PostgreSQL", "Aiogram"],
        created_at="сегодня",
        url="https://kwork.ru/projects/999999",
        platform="kwork",
        client_id="Client123"
    )
    
    print("="*60)
    print("ВХОД: ЧТО СКОРМИЛИ НЕЙРОСЕТИ (ПРОМПТ)")
    print("="*60)
    prompt = generator._build_user_prompt(project, "ru")
    print(prompt)
    
    print("\nГенерируем ответ (Groq LLM)...\n")
    response = await generator.generate(project)
    
    print("="*60)
    print("ВЫХОД: ЧТО ПРИДУМАЛА НЕЙРОСЕТЬ (ОТКЛИК)")
    print("="*60)
    print(response)
    
if __name__ == "__main__":
    asyncio.run(main())
