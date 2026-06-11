from pyrogram import Client, filters
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

# Учётные данные Telegram API берём только из окружения (.env): TELEGRAM_API_ID / TELEGRAM_API_HASH.
api_id_raw = os.getenv("TELEGRAM_API_ID", "")
api_hash = os.getenv("TELEGRAM_API_HASH", "")
if not api_id_raw.isdigit() or not api_hash:
    raise RuntimeError(
        "Не заданы TELEGRAM_API_ID / TELEGRAM_API_HASH. "
        "Укажите их в .env (получить можно на https://my.telegram.org)."
    )
api_id = int(api_id_raw)

async def main():
    # Подключаемся к уже созданному файлу сессии
    app = Client("arti_user_session", api_id=api_id, api_hash=api_hash)

    print("🚀 ARTI запущена и слушает сообщения...")
    print("Теперь открывай Телеграм на телефоне и запрашивай вход.")

    async with app:
        # Слушаем сообщения от официального сервисного аккаунта Telegram (ID 777000)
        @app.on_message(filters.service | filters.me | filters.private)
        async def my_handler(client, message):
            # Если сообщение пришло от Телеграма или содержит цифры (код)
            print(f"\n📩 НОВОЕ СООБЩЕНИЕ:")
            print(f"От: {message.from_user.first_name if message.from_user else 'Service'}")
            print(f"Текст: {message.text}")
            print("-" * 20)

        # Держим скрипт запущенным, пока ты не введешь код на телефоне
        while True:
            await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())