"""
Скрипт для первичного импорта файлов лора из директории lore/ в базу знаний memory_wiki_pages
Использование: python seed_wiki.py
"""
import asyncio
import os
import sys

# Настройка кодировки для Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(__file__))
from database.connection import init_db, close_db
from database.models import MemoryWikiPage

BASE_LORE = [
    {
        "filename": "default_personality.md",
        "page_key": "personality",
        "mode": "default",
        "title": "Личность Арти (Обычный режим)",
        "category": "personality",
    },
    {
        "filename": "rp_personality.md",
        "page_key": "personality",
        "mode": "rp",
        "title": "Личность Арти (Каноничный RP-режим)",
        "category": "personality",
    },
    {
        "filename": "rp_world_rules.md",
        "page_key": "rules",
        "mode": "rp",
        "title": "Правила отыгрыша и Законы вселенной",
        "category": "rp_rules",
    }
]

async def seed():
    print("=" * 60)
    print("Импорт файлов лора в базу знаний Wiki")
    print("=" * 60)
    
    lore_dir = os.path.join(os.path.dirname(__file__), "lore")
    if not os.path.exists(lore_dir):
        print(f"[ERROR] Директория {lore_dir} не найдена!")
        return

    # Инициализация пула БД
    await init_db()
    
    imported_count = 0
    try:
        for item in BASE_LORE:
            file_path = os.path.join(lore_dir, item["filename"])
            if not os.path.exists(file_path):
                print(f"[WARNING] Файл {item['filename']} отсутствует, пропускаем...")
                continue
                
            print(f"[INFO] Чтение {item['filename']}...")
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                
            if not content:
                print(f"[WARNING] Файл {item['filename']} пуст, пропускаем...")
                continue
                
            page_id = await MemoryWikiPage.save(
                page_key=item["page_key"],
                title=item["title"],
                content=content,
                category=item["category"],
                chat_id=None,  # Глобальная страница
                mode=item["mode"],
                importance=0.9,  # Базовый лор имеет высокую важность
                is_verified=True
            )
            
            if page_id:
                print(f"[OK] Страница '{item['page_key']}' ({item['mode']}) успешно импортирована! ID={page_id}")
                imported_count += 1
            else:
                print(f"[ERROR] Не удалось сохранить страницу '{item['page_key']}' ({item['mode']})")
                
        print("-" * 60)
        print(f"[SUCCESS] Импорт завершен! Успешно загружено страниц: {imported_count}/{len(BASE_LORE)}")
    except Exception as e:
        print(f"[CRITICAL] Произошла ошибка во время импорта: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await close_db()

if __name__ == "__main__":
    asyncio.run(seed())
