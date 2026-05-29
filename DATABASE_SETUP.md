# Настройка PostgreSQL для бота Арти

## Установка PostgreSQL

### Windows
1. Скачайте и установите PostgreSQL с официального сайта: https://www.postgresql.org/download/windows/
2. Запомните пароль для пользователя `postgres`
3. Убедитесь, что PostgreSQL запущен как служба

### Linux (Ubuntu/Debian)
```bash
sudo apt update
sudo apt install postgresql postgresql-contrib
sudo systemctl start postgresql
sudo systemctl enable postgresql
```

### macOS
```bash
brew install postgresql
brew services start postgresql
```

## Создание базы данных

### Подключение к PostgreSQL
```bash
# Windows (используйте pgAdmin или psql из установки)
psql -U postgres

# Linux/macOS
sudo -u postgres psql
```

### Создание базы данных и пользователя
```sql
-- Создаем базу данных
CREATE DATABASE arti_bot;

-- Создаем пользователя (опционально, можно использовать postgres)
CREATE USER arti_user WITH PASSWORD 'your_secure_password';

-- Даем права на базу данных
GRANT ALL PRIVILEGES ON DATABASE arti_bot TO arti_user;

-- Выходим
\q
```

## Настройка .env файла

Добавьте следующие переменные в файл `.env`:

```env
# PostgreSQL настройки
DB_HOST=localhost
DB_PORT=5432
DB_NAME=arti_bot
DB_USER=arti_user
DB_PASSWORD=your_secure_password

# Или используйте пользователя postgres по умолчанию:
# DB_USER=postgres
# DB_PASSWORD=your_postgres_password
```

## Установка зависимостей

```bash
pip install -r requirements_db.txt
```

или

```bash
pip install asyncpg
```

## Инициализация таблиц

Таблицы создаются автоматически при первом запуске бота. Если нужно создать их вручную:

```python
python -c "import asyncio; from database.connection import init_db, create_tables; asyncio.run(init_db()); asyncio.run(create_tables())"
```

## Миграция данных

Если у вас уже есть данные в памяти, они будут постепенно мигрировать в БД при первом использовании. Старые методы (in-memory) продолжают работать как fallback.

## Проверка подключения

```python
python -c "import asyncio; from database.connection import init_db, get_db; async def test(): await init_db(); async with get_db() as conn: print('Подключение успешно!'); asyncio.run(test())"
```

## Резервное копирование

```bash
# Создание бэкапа
pg_dump -U postgres arti_bot > backup.sql

# Восстановление
psql -U postgres arti_bot < backup.sql
```

## Производительность

Для оптимизации производительности рекомендуется:
- Использовать индексы (создаются автоматически)
- Настроить `max_size` пула соединений в `database/connection.py` в зависимости от нагрузки
- Регулярно очищать старые записи (автоматически для chat_history и dialog_history)

