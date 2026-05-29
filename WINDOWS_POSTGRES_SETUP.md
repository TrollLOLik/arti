# Установка PostgreSQL на Windows

## Вариант 1: Установка PostgreSQL (рекомендуется)

### Шаг 1: Скачать установщик
1. Перейдите на https://www.postgresql.org/download/windows/
2. Нажмите на "Download the installer"
3. Выберите версию (рекомендуется 15 или 16)
4. Выберите установщик для вашей системы (64-bit)

### Шаг 2: Установка
1. Запустите установщик
2. **Важно:** Во время установки запомните пароль для пользователя `postgres` (будет предложено установить)
3. Порт по умолчанию: `5432` (можно оставить как есть)
4. Убедитесь, что отмечено "Add PostgreSQL to PATH" (или добавьте вручную после установки)

### Шаг 3: Добавить в PATH (если не было отмечено)
1. Найдите установку PostgreSQL (обычно `C:\Program Files\PostgreSQL\16\bin`)
2. Добавьте путь в PATH:
   - Откройте "Система" → "Дополнительные параметры системы" → "Переменные среды"
   - Найдите `Path` в "Системные переменные"
   - Нажмите "Изменить" → "Создать"
   - Добавьте: `C:\Program Files\PostgreSQL\16\bin` (замените 16 на вашу версию)
   - Нажмите "OK" везде
3. Перезапустите PowerShell/Terminal

### Шаг 4: Проверка
```powershell
psql --version
```

## Вариант 2: Использование Docker (быстрее, если Docker установлен)

### Установка Docker Desktop (если нет)
1. Скачайте Docker Desktop: https://www.docker.com/products/docker-desktop
2. Установите и перезагрузите компьютер

### Запуск PostgreSQL в Docker
```powershell
# Запустить PostgreSQL контейнер
docker run --name arti-postgres `
  -e POSTGRES_PASSWORD=your_password `
  -e POSTGRES_DB=arti_bot `
  -p 5432:5432 `
  -d postgres:16

# Проверить, что контейнер запущен
docker ps
```

### Подключение к PostgreSQL в Docker
```powershell
# Войти в контейнер
docker exec -it arti-postgres psql -U postgres

# Или с хоста (если psql установлен)
psql -h localhost -U postgres -d arti_bot
```

## Вариант 3: Использование pgAdmin (GUI инструмент)

1. При установке PostgreSQL обычно устанавливается pgAdmin
2. Запустите pgAdmin
3. Подключитесь к серверу (пароль который вы задали при установке)
4. Создайте базу данных `arti_bot` через интерфейс

## Вариант 4: Создание БД через Python (без psql)

Создайте файл `setup_database.py`:

```python
import asyncio
import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()

async def setup_database():
    # Подключаемся к postgres БД для создания новой БД
    db_host = os.getenv("DB_HOST", "localhost")
    db_port = int(os.getenv("DB_PORT", "5432"))
    db_name = os.getenv("DB_NAME", "arti_bot")
    db_user = os.getenv("DB_USER", "postgres")
    db_password = os.getenv("DB_PASSWORD", "")
    
    if not db_password:
        db_password = input("Введите пароль для postgres: ")
    
    try:
        # Подключаемся к стандартной БД postgres
        conn = await asyncpg.connect(
            host=db_host,
            port=db_port,
            database="postgres",
            user=db_user,
            password=db_password
        )
        
        # Проверяем, существует ли БД
        exists = await conn.fetchval("""
            SELECT 1 FROM pg_database WHERE datname = $1
        """, db_name)
        
        if not exists:
            # Создаем БД
            await conn.execute(f'CREATE DATABASE "{db_name}"')
            print(f"База данных {db_name} создана успешно!")
        else:
            print(f"База данных {db_name} уже существует")
        
        await conn.close()
        
        # Подключаемся к новой БД и создаем таблицы
        conn = await asyncpg.connect(
            host=db_host,
            port=db_port,
            database=db_name,
            user=db_user,
            password=db_password
        )
        
        # Создаем таблицы
        from database.connection import create_tables
        await create_tables()
        
        await conn.close()
        print("Таблицы созданы успешно!")
        
    except Exception as e:
        print(f"Ошибка: {e}")
        print("\nУбедитесь, что:")
        print("1. PostgreSQL установлен и запущен")
        print("2. Пароль введен правильно")
        print("3. Параметры подключения в .env файле корректны")

if __name__ == "__main__":
    asyncio.run(setup_database())
```

Запустите:
```powershell
python setup_database.py
```

## Быстрый старт (рекомендуется)

1. **Установите PostgreSQL** (Вариант 1) или используйте **Docker** (Вариант 2)
2. Добавьте в `.env`:
   ```env
   DB_HOST=localhost
   DB_PORT=5432
   DB_NAME=arti_bot
   DB_USER=postgres
   DB_PASSWORD=ваш_пароль_от_postgres
   ```
3. **Запустите скрипт настройки** (Вариант 4) или создайте БД вручную
4. Запустите бота - таблицы создадутся автоматически!

## Проверка установки PostgreSQL

```powershell
# Проверить, запущена ли служба PostgreSQL
Get-Service -Name postgresql*

# Или через Services (services.msc)
# Найдите службу с именем "postgresql-x64-XX"
```

Если служба не запущена, запустите её:
```powershell
Start-Service postgresql-x64-16  # замените на вашу версию
```

