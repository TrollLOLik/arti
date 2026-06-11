"""
Скрипт для автоматической настройки базы данных PostgreSQL
Использование: python setup_database.py
"""
import asyncio
import asyncpg
import os
import re
import sys
from dotenv import load_dotenv

# Настройка кодировки для Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

load_dotenv()

async def setup_database():
    """Создает базу данных и таблицы"""
    # Параметры подключения
    db_host = os.getenv("DB_HOST", "localhost")
    db_port = int(os.getenv("DB_PORT", "5432"))
    db_name = os.getenv("DB_NAME", "arti_bot")
    db_user = os.getenv("DB_USER", "postgres")
    db_password = os.getenv("DB_PASSWORD", "")

    # Имя БД попадает в неэкранируемый CREATE DATABASE — допускаем только
    # безопасный идентификатор, чтобы исключить SQL injection через DB_NAME.
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", db_name):
        print(f"[ERROR] Недопустимое имя базы данных DB_NAME={db_name!r}. "
              f"Разрешены латинские буквы, цифры и подчёркивание; первый символ — буква или _.")
        return False

    if not db_password:
        print("[WARNING] DB_PASSWORD not found in .env file")
        try:
            db_password = input("Enter password for postgres user: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[ERROR] Input cancelled")
            return False
        if not db_password:
            print("[ERROR] Password cannot be empty")
            return False
    
    try:
        print(f"[INFO] Connecting to PostgreSQL at {db_host}:{db_port}...")
        
        # Подключаемся к стандартной БД postgres для создания новой БД
        conn = await asyncpg.connect(
            host=db_host,
            port=db_port,
            database="postgres",
            user=db_user,
            password=db_password,
            timeout=10
        )
        
        print("[OK] Connection to PostgreSQL successful!")
        
        # Проверяем, существует ли БД
        exists = await conn.fetchval("""
            SELECT 1 FROM pg_database WHERE datname = $1
        """, db_name)
        
        if not exists:
            # Создаем БД
            print(f"[INFO] Creating database {db_name}...")
            await conn.execute(f'CREATE DATABASE "{db_name}"')
            print(f"[OK] Database {db_name} created successfully!")
        else:
            print(f"[INFO] Database {db_name} already exists")
        
        await conn.close()
        
        # Подключаемся к новой БД и создаем таблицы
        print(f"[INFO] Connecting to database {db_name}...")
        conn = await asyncpg.connect(
            host=db_host,
            port=db_port,
            database=db_name,
            user=db_user,
            password=db_password,
            timeout=10
        )
        
        print("[OK] Connection to database successful!")
        
        # Импортируем и выполняем создание таблиц
        sys.path.insert(0, os.path.dirname(__file__))
        from database.connection import create_tables
        
        print("[INFO] Creating tables...")
        await create_tables(conn=conn)  # Передаем существующее соединение
        
        await conn.close()
        print("[OK] All tables created successfully!")
        print("\n[SUCCESS] Database setup completed!")
        print("\nNow you can run the bot:")
        print("  python main.py")
        
        return True
        
    except asyncpg.InvalidPasswordError:
        print("[ERROR] Invalid password for postgres user")
        print("\nCheck:")
        print("  1. Password in .env file is correct")
        print("  2. Password you set during PostgreSQL installation")
        return False
    except (asyncpg.PostgresConnectionError, ConnectionRefusedError, OSError) as e:
        print(f"[ERROR] Cannot connect to PostgreSQL")
        print(f"Details: {e}")
        print("\nMake sure:")
        print("  1. PostgreSQL is installed and running")
        print("  2. PostgreSQL service is started (check in services.msc)")
        print("  3. Port 5432 is not used by another application")
        print("  4. DB_HOST and DB_PORT in .env are correct")
        print("\nIf PostgreSQL is not installed, see WINDOWS_POSTGRES_SETUP.md")
        return False
    except Exception as e:
        print(f"[ERROR] Error: {e}")
        print(f"Error type: {type(e).__name__}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("=" * 60)
    print("Setup PostgreSQL database for Arti bot")
    print("=" * 60)
    print()
    
    success = asyncio.run(setup_database())
    
    if not success:
        print("\n" + "=" * 60)
        print("TIP: If PostgreSQL is not installed, see WINDOWS_POSTGRES_SETUP.md")
        print("=" * 60)
        sys.exit(1)

