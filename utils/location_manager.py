"""
Менеджер геолокации пользователей.
Хранение и согласие ограничены пользователем, чатом и темой.
TTL: 30 минут после последней явно полученной геопозиции.
"""
import time
import logging
import asyncio
import math
import uuid

import aiohttp

from utils import location_store
from utils.location_scope import LOCATION_TTL_SECONDS, location_scope_key, expire_pending_map_requests

logger = logging.getLogger(__name__)

# Keys are (chat_id, topic_id, user_id), never a global user ID.
_location_cache = {}

LIVE_TTL_SECONDS = LOCATION_TTL_SECONDS
STATIC_TTL_SECONDS = LOCATION_TTL_SECONDS

# REL-05: троттлинг обратного геокодирования (Nominatim usage policy: ~1 req/s).
# Live-геолокация шлёт апдейты каждые несколько секунд — без троттлинга легко
# словить бан IP. Геокодируем не чаще раза в GEOCODE_MIN_INTERVAL на пользователя
# и глобально разносим запросы минимум на GEOCODE_GLOBAL_SPACING секунд.
GEOCODE_MIN_INTERVAL = 120.0
GEOCODE_GLOBAL_SPACING = 1.1
_last_geocode_at: dict = {}            # scope key -> monotonic
_geocode_global_lock = None            # ленивый asyncio.Lock
_geocode_last_global = 0.0
_geocode_tasks = set()
_geocoding_enabled = True


def _geocode_done(task):
    _geocode_tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning('Location geocoding task failed')


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

                return {"city": city, "address": address}
    except asyncio.TimeoutError:
        logger.warning("Nominatim: таймаут")
    except Exception as e:
        logger.warning("Nominatim: ошибка геокодирования")

    return {"city": None, "address": None}


def _fresh(sample):
    stamp = sample.get('timestamp') if isinstance(sample, dict) else None
    return isinstance(stamp, (int, float)) and 0 <= time.time() - stamp < LOCATION_TTL_SECONDS


def _public_location(sample):
    return {key: sample.get(key) for key in ('lat', 'lng', 'city', 'address')}


def _expire_cache():
    for key, sample in list(_location_cache.items()):
        if not isinstance(key, tuple) or len(key) != 3 or not _fresh(sample):
            _location_cache.pop(key, None)
            _last_geocode_at.pop(key, None)


async def set_user_location(user_id: int, lat: float, lng: float, is_live: bool = False,
                            *, chat_id=None, scope=None, shared_at=None):
    """Save only an explicit share in the receiving scope; never renew old input."""
    key = location_scope_key(user_id, chat_id=chat_id, scope=scope)
    if key is None or not math.isfinite(lat) or not math.isfinite(lng) or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return False
    if shared_at is None:
        shared_at = time.time()
    elif hasattr(shared_at, 'timestamp'):
        shared_at = shared_at.timestamp()
    _expire_cache()
    prev = _location_cache.get(key) or {}
    if not _fresh({'timestamp': shared_at}) or prev.get('timestamp', 0) > shared_at:
        return False
    mono = time.monotonic()
    should_geocode = mono - _last_geocode_at.get(key, -GEOCODE_MIN_INTERVAL) >= GEOCODE_MIN_INTERVAL
    sample = dict(lat=lat, lng=lng, city=None if should_geocode else prev.get('city'),
                  address=None if should_geocode else prev.get('address'),
                  timestamp=shared_at, live=is_live, sample_id=uuid.uuid4().hex)
    # Publish before an await, so an older concurrent sample cannot replace it.
    _location_cache[key] = sample
    try:
        saved = await location_store.save(key, sample)
        if saved is None:
            if _location_cache.get(key) is sample:
                _location_cache.pop(key, None)
            return False
    except Exception:
        logger.warning('Location persistence unavailable; scoped cache only')
    if should_geocode and _geocoding_enabled and _location_cache.get(key) is sample:
        _last_geocode_at[key] = mono
        task = asyncio.create_task(_do_geocoding(key, sample))
        _geocode_tasks.add(task)
        task.add_done_callback(_geocode_done)
    return True


async def _do_geocoding(key, sample):
    """A late address may update only the same unexpired sample, without renewal."""
    result = await _reverse_geocode(sample['lat'], sample['lng'])
    if not _fresh(sample) or _location_cache.get(key) is not sample:
        return
    city, address = result.get('city'), result.get('address')
    if city or address:
        try:
            await location_store.update_address(key, sample['sample_id'], city=city, address=address)
        except Exception:
            logger.warning('Location address persistence unavailable')
        if _fresh(sample) and _location_cache.get(key) is sample:
            sample.update(city=city, address=address)


async def get_user_location(user_id: int, *, chat_id=None, scope=None) -> dict | None:
    """Return only a fresh share in this exact scope, preserving its original age."""
    key = location_scope_key(user_id, chat_id=chat_id, scope=scope)
    _expire_cache()
    if key is None:
        return None
    cache = _location_cache.get(key)
    if cache:
        return _public_location(cache)
    try:
        sample = await location_store.get(key)
        if sample and _fresh(sample):
            # A newer share may have arrived while the read was in flight.
            cache = _location_cache.get(key)
            if cache and _fresh(cache) and cache['timestamp'] >= sample['timestamp']:
                return _public_location(cache)
            _location_cache[key] = sample
            return _public_location(sample)
    except Exception:
        logger.warning('Location lookup unavailable')
    return None


async def maintenance_worker():
    """Read-time expiry is immediate; physical cleanup runs at least each minute."""
    global _geocoding_enabled
    _geocoding_enabled = True
    try:
        while True:
            _expire_cache()
            expire_pending_map_requests()
            await location_store.expire()
            await asyncio.sleep(60)
    finally:
        _geocoding_enabled = False
        tasks = tuple(_geocode_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            from utils.async_cleanup import await_owned
            await await_owned(asyncio.gather(*tasks, return_exceptions=True))


async def get_user_location_context(user_id: int, *, chat_id=None, scope=None) -> str:
    """
    Формирует строку с локацией для вставки в системный промпт.
    Пустая строка, если локации нет.
    """
    loc = await get_user_location(user_id, chat_id=chat_id, scope=scope)
    return format_location_context(loc)


def format_location_context(loc) -> str:
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
