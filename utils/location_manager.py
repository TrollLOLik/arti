"""
Менеджер геолокации пользователей.
DB-first хранение (PostgreSQL) + in-memory кеш + Nominatim reverse geocoding.
TTL: 4 часа для live-геопозиций, 2 часа для статических.
"""
import time
import logging
import asyncio

import aiohttp

from database.models import UserLocation as UserLocationModel

logger = logging.getLogger(__name__)

# In-memory кеш для быстрого доступа: {user_id: {"lat": float, "lng": float, "city": str, "address": str, "timestamp": float, "live": bool}}
_location_cache = {}

LIVE_TTL_SECONDS = 4 * 60 * 60      # 4 часа для live-локаций
STATIC_TTL_SECONDS = 2 * 60 * 60    # 2 часа для статических

# REL-05: троттлинг обратного геокодирования (Nominatim usage policy: ~1 req/s).
# Live-геолокация шлёт апдейты каждые несколько секунд — без троттлинга легко
# словить бан IP. Геокодируем не чаще раза в GEOCODE_MIN_INTERVAL на пользователя
# и глобально разносим запросы минимум на GEOCODE_GLOBAL_SPACING секунд.
GEOCODE_MIN_INTERVAL = 120.0
GEOCODE_GLOBAL_SPACING = 1.1
_last_geocode_at: dict = {}            # user_id -> monotonic
_geocode_global_lock = None            # ленивый asyncio.Lock
_geocode_last_global = 0.0


async def _reverse_geocode(lat: float, lng: float) -> dict:
    """
    Обратное геокодирование через Nominatim (OpenStreetMap).
    Возвращает {"city": str, "address": str} или {"city": None, "address": None}.
    """
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {
        "lat": lat,
        "lon": lng,
        "format": "json",
        "zoom": 18,
        "accept-language": "ru",
    }
    headers = {"User-Agent": "ArtiBot/1.0 (telegram bot)"}

    # REL-05: глобально разносим запросы к Nominatim (>= ~1 req/s по их policy).
    global _geocode_global_lock, _geocode_last_global
    if _geocode_global_lock is None:
        _geocode_global_lock = asyncio.Lock()
    async with _geocode_global_lock:
        wait = GEOCODE_GLOBAL_SPACING - (time.monotonic() - _geocode_last_global)
        if wait > 0:
            await asyncio.sleep(wait)
        _geocode_last_global = time.monotonic()

    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning(f"Nominatim вернул {resp.status}")
                    return {"city": None, "address": None}
                data = await resp.json()

                address = data.get("display_name")
                addr_details = data.get("address", {})

                # Пытаемся вытащить город разными ключами
                city = (
                    addr_details.get("city")
                    or addr_details.get("town")
                    or addr_details.get("village")
                    or addr_details.get("hamlet")
                    or addr_details.get("county")
                    or addr_details.get("state")
                )

                logger.info(f"🌍 Nominatim: {city} | {address}")
                return {"city": city, "address": address}
    except asyncio.TimeoutError:
        logger.warning("Nominatim: таймаут")
    except Exception as e:
        logger.warning(f"Nominatim: ошибка геокодирования: {e}")

    return {"city": None, "address": None}


async def set_user_location(user_id: int, lat: float, lng: float, is_live: bool = False):
    """
    Сохраняет геопозицию в БД и кеш, затем запускает фоновое геокодирование.
    """
    now = time.time()
    ttl = LIVE_TTL_SECONDS if is_live else STATIC_TTL_SECONDS

    # REL-05: решаем, нужно ли геокодировать (троттлинг per-user).
    mono = time.monotonic()
    should_geocode = (mono - _last_geocode_at.get(user_id, 0.0)) >= GEOCODE_MIN_INTERVAL

    # Сохраняем в БД без адреса (сначала)
    try:
        await UserLocationModel.save(user_id, lat, lng, address=None, city=None)
    except Exception as e:
        logger.error(f"Ошибка сохранения локации в БД: {e}")

    # Если геокодирование сейчас пропускаем — сохраняем ранее определённый адрес,
    # чтобы не «обнулять» город/адрес на каждом live-апдейте.
    prev = _location_cache.get(user_id) or {}
    carry_city = None if should_geocode else prev.get("city")
    carry_address = None if should_geocode else prev.get("address")

    # Обновляем in-memory кеш
    _location_cache[user_id] = {
        "lat": lat,
        "lng": lng,
        "city": carry_city,
        "address": carry_address,
        "timestamp": now,
        "live": is_live,
        "ttl": ttl,
    }
    logger.info(f"📍 Геопозиция пользователя {user_id} обновлена: {lat:.5f}, {lng:.5f} (live={is_live})")

    # Фоновое геокодирование (с троттлингом — Nominatim usage policy).
    if should_geocode:
        _last_geocode_at[user_id] = mono
        asyncio.create_task(_do_geocoding(user_id, lat, lng))


async def _do_geocoding(user_id: int, lat: float, lng: float):
    """Фоновая задача: получить адрес и обновить БД + кеш."""
    result = await _reverse_geocode(lat, lng)
    city = result.get("city")
    address = result.get("address")

    if city or address:
        try:
            await UserLocationModel.update_address(user_id, address, city)
        except Exception as e:
            logger.error(f"Ошибка обновления адреса в БД: {e}")

        # Обновляем кеш
        cache = _location_cache.get(user_id)
        if cache:
            cache["city"] = city
            cache["address"] = address
            logger.info(f"🌍 Адрес для {user_id} обновлён: {city}")


async def get_user_location(user_id: int) -> dict | None:
    """
    Возвращает геопозицию {"lat", "lng", "city", "address"} если не протухла.
    Порядок: кеш → БД (с TTL).
    """
    now = time.time()

    # 1. Проверяем in-memory кеш
    cache = _location_cache.get(user_id)
    if cache:
        age = now - cache["timestamp"]
        ttl = cache.get("ttl", STATIC_TTL_SECONDS)
        if age <= ttl:
            return {
                "lat": cache["lat"],
                "lng": cache["lng"],
                "city": cache.get("city"),
                "address": cache.get("address"),
            }
        else:
            del _location_cache[user_id]
            logger.info(f"📍 Кеш локации {user_id} протух ({age:.0f}с)")

    # 2. Проверяем БД (4-часовой TTL по умолчанию)
    try:
        db_loc = await UserLocationModel.get_with_ttl(user_id, ttl_seconds=LIVE_TTL_SECONDS)
        if db_loc:
            _location_cache[user_id] = {
                "lat": db_loc["lat"],
                "lng": db_loc["lng"],
                "city": db_loc.get("city"),
                "address": db_loc.get("address"),
                "timestamp": now,
                "live": False,
                "ttl": STATIC_TTL_SECONDS,
            }
            logger.info(f"📍 Локация {user_id} восстановлена из БД")
            return {
                "lat": db_loc["lat"],
                "lng": db_loc["lng"],
                "city": db_loc.get("city"),
                "address": db_loc.get("address"),
            }
    except Exception as e:
        logger.error(f"Ошибка чтения локации из БД: {e}")

    return None


async def get_user_location_context(user_id: int) -> str:
    """
    Формирует строку с локацией для вставки в системный промпт.
    Пустая строка, если локации нет.
    """
    loc = await get_user_location(user_id)
    if not loc:
        return ""

    city = loc.get("city") or "неизвестный город"
    lat = loc["lat"]
    lng = loc["lng"]

    return (
        f"[ГЕОЛОКАЦИЯ ПОЛЬЗОВАТЕЛЯ]: Текущее местоположение: {city}, "
        f"координаты {lat:.5f}, {lng:.5f}. "
        f"Если запрос связан с местами, маршрутами, расстояниями, досугом или навигацией — "
        f"используй эти данные как точку отсчёта."
    )


def clear_user_location(user_id: int):
    """Удаляет геопозицию из кеша и БД."""
    _location_cache.pop(user_id, None)
    # Асинхронное удаление из БД — запускаем в фоне
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(UserLocationModel.save(user_id, 0.0, 0.0))
    except Exception:
        pass


def cleanup_expired():
    """Удаление просроченных записей из кеша."""
    now = time.time()
    expired = [
        uid for uid, data in _location_cache.items()
        if now - data["timestamp"] > data.get("ttl", STATIC_TTL_SECONDS)
    ]
    for uid in expired:
        del _location_cache[uid]
    if expired:
        logger.debug(f"📍 Очищено {len(expired)} просроченных геопозиций из кеша")
