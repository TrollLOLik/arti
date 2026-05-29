from pyrogram import Client, filters
import asyncio

# Используй те же данные, через которые зашла успешно
api_id = 2040
api_hash = "b18441a1ff607e10a989891a5462e627"

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