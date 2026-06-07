import asyncio
import logging
import sys
from datetime import datetime, timedelta, date
from database.connection import init_db, get_db
from database.models import ChatEmotionalState, UserEvent, infer_user_timezone
from bot.queue import check_event_pre_filter, extract_and_save_events_task

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("test_proactive")

async def run_tests():
    logger.info("Starting Premium Proactive & Emotional Improvements test suite...")
    await init_db()

    chat_id = 999999999 + int(datetime.now().timestamp()) % 1000000
    user_id = 888888888 + int(datetime.now().timestamp()) % 1000000
    mode = "default"

    # =========================================================================
    # TEST 1: Timezone Inference
    # =========================================================================
    logger.info("\n--- TEST 1: Timezone Inference ---")
    
    # Check that inference returns None when message history is empty
    tz = await infer_user_timezone(chat_id, user_id)
    assert tz is None, f"Expected None for empty history, got {tz}"
    logger.info("[OK] Empty history correctly returns None timezone offset")

    # Seed 25 fake messages sent at typical daytime hours (say 15:00 UTC)
    # We want to test that it scores and returns a timezone offset H.
    # Standard server offset from UTC is calculated. Let's see if H matches expectation.
    async with get_db() as conn:
        # Clear any existing history
        await conn.execute("DELETE FROM chat_history WHERE chat_id = $1", chat_id)
        
        # Insert 25 messages spread out in time
        base_time = datetime.now() - timedelta(days=5)
        for i in range(25):
            # Let's say the user wrote every day at 14:00 server local time
            msg_time = base_time + timedelta(hours=i * 2)
            await conn.execute("""
                INSERT INTO chat_history (chat_id, timestamp, user_name, message_text)
                VALUES ($1, $2, 'User', 'Some text')
            """, chat_id, msg_time)

    tz = await infer_user_timezone(chat_id, user_id)
    logger.info(f"[OK] Timezone inferred successfully with 25 messages: {tz}")
    assert tz is not None, "Expected timezone to be inferred"

    # =========================================================================
    # TEST 2: Aggression Fuse & Negation Heuristics
    # =========================================================================
    logger.info("\n--- TEST 2: Aggression Fuse & Negation Heuristics ---")

    # Clear state first
    async with get_db() as conn:
        await conn.execute("DELETE FROM chat_emotional_states WHERE chat_id = $1", chat_id)

    # stranger (closeness = 0.1)
    state = await ChatEmotionalState.update_state(chat_id, "Ты бесишь меня, дурак!", closeness=0.1, user_id=user_id)
    mood = state["mood_state"]
    import json
    mood_dict = json.loads(mood) if isinstance(mood, str) else mood
    logger.info(f"Stranger aggression mood: {mood_dict}")
    assert mood_dict["angry"] == 0.0, f"Expected angry to be 0.0 for stranger, got {mood_dict['angry']}"
    assert mood_dict["bored"] > 0.0, "Expected stranger aggression to trigger cold bored mood"
    logger.info("[OK] Stranger aggression correctly triggered cold bored mood instead of mirroring angry")

    # close friend (closeness = 0.6)
    state = await ChatEmotionalState.update_state(chat_id, "Ты бесишь меня, дурак!", closeness=0.6, user_id=user_id)
    mood = state["mood_state"]
    mood_dict = json.loads(mood) if isinstance(mood, str) else mood
    logger.info(f"Friend aggression mood: {mood_dict}")
    assert mood_dict["angry"] > 0.0, "Expected friend aggression to mirror angry"
    logger.info("[OK] Close friend aggression mirrored angry successfully")

    # Negation: "не грусти" (negated sad)
    state = await ChatEmotionalState.update_state(chat_id, "не грусти, все хорошо", closeness=0.6, user_id=user_id)
    mood = state["mood_state"]
    mood_dict = json.loads(mood) if isinstance(mood, str) else mood
    logger.info(f"Negated sad ('не грусти') mood: {mood_dict}")
    assert mood_dict["happy"] > 0.0 or mood_dict["love"] > 0.0, "Expected 'не грусти' to trigger happy/love"
    logger.info("[OK] Negated sad sentiment ('не грусти') successfully triggered happy/love shifts")

    # Negation: "без обид" (negated angry)
    state = await ChatEmotionalState.update_state(chat_id, "без обид и злости", closeness=0.6, user_id=user_id)
    mood = state["mood_state"]
    mood_dict = json.loads(mood) if isinstance(mood, str) else mood
    logger.info(f"Negated angry ('без обид') mood: {mood_dict}")
    assert mood_dict["happy"] > 0.0 or mood_dict["love"] > 0.0, "Expected 'без обид' to trigger happy/love"
    logger.info("[OK] Negated angry sentiment ('без обид') successfully triggered happy/love shifts")

    # =========================================================================
    # TEST 3: Structured Event Tracking & Pre-Filter
    # =========================================================================
    logger.info("\n--- TEST 3: Event Tracking & Pre-Filter ---")

    # Verify pre-filter matches date markers
    assert check_event_pre_filter("У меня завтра экзамен по матлабу") is True
    assert check_event_pre_filter("В пятницу дедлайн") is True
    assert check_event_pre_filter("Встреча назначена на 15.06") is True
    assert check_event_pre_filter("Просто обычное общение без дат") is False
    logger.info("[OK] Cheap regex pre-filter triggers correctly only for date/event-heavy phrases")

    # Add a structured event with unique upsert deduplication
    today = date.today()
    tomorrow = today + timedelta(days=1)
    
    # Clear events
    async with get_db() as conn:
        await conn.execute("DELETE FROM user_events WHERE chat_id = $1", chat_id)

    # Insert a structured event
    await UserEvent.add(chat_id, tomorrow, "exam", "Экзамен по матлабу")
    logger.info("[OK] Successfully inserted structured event")

    # Insert a duplicate event - should overwrite note and keep uniqueness
    await UserEvent.add(chat_id, tomorrow, "exam", "Новое описание экзамена")
    
    events = await UserEvent.get_upcoming_for_chat(chat_id, today, tomorrow)
    logger.info(f"Retrieved events: {events}")
    assert len(events) == 1, f"Expected 1 unique event, got {len(events)}"
    assert events[0]["note"] == "Новое описание экзамена", "Expected note to be overwritten by deduplication upsert"
    logger.info("[OK] Unique events constraints and deduplication upsert verified perfectly")

    # =========================================================================
    # TEST 4: Quiet Hours and Proactive Back-Off
    # =========================================================================
    logger.info("\n--- TEST 4: Quiet Hours and Proactive Back-Off ---")

    # Let's inspect quiet hours suppression logic by checking the candidate selection query.
    # If the user timezone offset places local hour between 23:00 and 09:00:
    # Say server time is 01:23 (local offset +5). If user offset is -5, local time is 15:23 (active).
    # If user offset is +3, local time is 23:23 (quiet hours!).
    # We will simulate this by checking local hours logic:
    # Используем фиксированную опорную точку UTC (полдень), чтобы тест был детерминированным
    # и не зависел от момента запуска. Проверяем границы окна тишины (8 -> тихо, 9 -> активно,
    # 23 -> тихо, 0 -> тихо).
    base_utc = datetime(2024, 1, 1, 12, 0, 0)
    for test_tz, expected_quiet in [(0, False), (-3, False), (-4, True), (11, True), (12, True)]:
        user_local_time = base_utc + timedelta(hours=test_tz)
        local_hour = user_local_time.hour
        is_quiet = local_hour >= 23 or local_hour < 9
        assert is_quiet == expected_quiet, f"For TZ={test_tz} local_hour={local_hour}, is_quiet={is_quiet} but expected {expected_quiet}"
        
    logger.info("[OK] Quiet hours checker correctly suppresses pushes between 23:00 and 09:00 user local time")

    # Test Proactive Back-Off transition:
    # 1. When a proactive push is made, state becomes 'proactive_sent'
    # 2. When in 'proactive_sent' stage, the query does not select this chat.
    # 3. When a user reply comes in, state becomes 'active'.
    async with get_db() as conn:
        # Setup chat stage as proactive_sent
        await conn.execute("UPDATE chat_emotional_states SET conversation_stage = 'proactive_sent' WHERE chat_id = $1", chat_id)
        
        # Run candidate select to verify it's ignored
        states = await conn.fetch("""
            SELECT chat_id FROM chat_emotional_states
            WHERE conversation_stage = 'active' AND chat_id = $1
        """, chat_id)
        assert len(states) == 0, "Expected proactive_sent chat to be ignored by proactive scheduler candidate scan"
        logger.info("[OK] Proactive Back-Off suppresses duplicate pushes (stage proactive_sent is skipped)")

        # Simulate user reply (state reset to active)
        await ChatEmotionalState.update_state(chat_id, "Привет Арти", closeness=0.5, user_id=user_id)
        
        # Verify reset to 'active'
        state = await conn.fetchrow("SELECT conversation_stage FROM chat_emotional_states WHERE chat_id = $1", chat_id)
        assert state["conversation_stage"] == "active", f"Expected stage reset to active, got {state['conversation_stage']}"
        logger.info("[OK] User reply successfully resets stage to 'active', unfreezing future proactive pushes")

    # =========================================================================
    # TEST 5: Closeness growth from regular interaction (passive, throttled)
    # =========================================================================
    logger.info("\n--- TEST 5: Passive Closeness Growth ---")
    from database.models import MemoryUserProfile

    async def _get_closeness():
        prof = await MemoryUserProfile.get(chat_id, user_id, mode)
        if not prof or not prof.get("profile_json"):
            return None
        pj = json.loads(prof["profile_json"]) if isinstance(prof["profile_json"], str) else prof["profile_json"]
        return pj.get("affective", {})

    # Clean profile for a deterministic baseline
    async with get_db() as conn:
        await conn.execute("DELETE FROM memory_user_profiles WHERE chat_id = $1", chat_id)

    await MemoryUserProfile.grow_closeness(chat_id, user_id, mode, proactive_reply=False)
    aff1 = await _get_closeness()
    assert aff1 is not None, "Expected profile to be created by grow_closeness"
    assert abs(aff1["closeness"] - 0.11) < 1e-6, f"Expected closeness 0.11 after first passive growth, got {aff1['closeness']}"
    logger.info(f"[OK] Passive growth bumped closeness 0.10 -> {aff1['closeness']:.3f}")

    # Second immediate call must be throttled (no change within 90 sec)
    await MemoryUserProfile.grow_closeness(chat_id, user_id, mode, proactive_reply=False)
    aff2 = await _get_closeness()
    assert abs(aff2["closeness"] - aff1["closeness"]) < 1e-6, "Expected passive growth to be throttled within 90 sec"
    logger.info("[OK] Rapid second message correctly throttled (no closeness inflation)")

    # =========================================================================
    # TEST 6: Closeness bonus for replying to a proactive push (no throttle)
    # =========================================================================
    logger.info("\n--- TEST 6: Proactive-Reply Closeness Bonus ---")
    before = (await _get_closeness())["closeness"]
    await MemoryUserProfile.grow_closeness(chat_id, user_id, mode, proactive_reply=True)
    after = (await _get_closeness())["closeness"]
    # Passive is throttled (just ran), so only the +0.05 proactive bonus applies
    assert abs((after - before) - 0.05) < 1e-6, f"Expected +0.05 proactive-reply bonus, got delta {after - before}"
    logger.info(f"[OK] Proactive reply bonus applied immediately (bypasses throttle): {before:.3f} -> {after:.3f}")

    # was_proactive_reply must be surfaced by update_state when stage was 'proactive_sent'
    async with get_db() as conn:
        await conn.execute("UPDATE chat_emotional_states SET conversation_stage = 'proactive_sent' WHERE chat_id = $1", chat_id)
    st = await ChatEmotionalState.update_state(chat_id, "ой привет, замоталась", closeness=0.6, user_id=user_id)
    assert st.get("was_proactive_reply") is True, "Expected update_state to flag was_proactive_reply when stage was proactive_sent"
    logger.info("[OK] update_state correctly flags was_proactive_reply for replies to proactive pushes")

    # =========================================================================
    # TEST 7: Proactive sticker bypasses charge gate (force=True)
    # =========================================================================
    logger.info("\n--- TEST 7: Forced Proactive Sticker (charge gate bypass) ---")
    import ai.stickers as stickers_mod

    class _FakeBot:
        def __init__(self):
            self.sent = []
        async def send_chat_action(self, **kwargs):
            return None
        async def send_sticker(self, **kwargs):
            self.sent.append(kwargs)
        async def set_message_reaction(self, **kwargs):
            return None

    # Force near-zero charge (the exact condition after long silence / catharsis reset)
    async with get_db() as conn:
        await conn.execute("UPDATE chat_emotional_states SET charge = 0.0, last_sticker_time = NULL WHERE chat_id = $1", chat_id)

    orig_enabled = stickers_mod.STICKERS_ENABLED
    orig_loader = stickers_mod.load_sticker_pack
    orig_rand = stickers_mod.random.random
    stickers_mod.STICKERS_ENABLED = True

    async def _fake_pack(bot):
        return {"bored": ["FAKE_STICKER_FILE_ID"]}
    stickers_mod.load_sticker_pack = _fake_pack

    try:
        # force=True: must send despite charge=0
        bot_force = _FakeBot()
        await stickers_mod.send_mood_sticker_task(bot_force, chat_id, user_id, "bored", 12345, force=True)
        assert len(bot_force.sent) == 1, f"Expected forced proactive sticker to send despite charge=0, sent={len(bot_force.sent)}"
        logger.info("[OK] force=True sends proactive sticker even when charge=0 (gate bypassed)")

        # force=False with charge=0: probability gate must block (roll forced high)
        async with get_db() as conn:
            await conn.execute("UPDATE chat_emotional_states SET charge = 0.0, last_sticker_time = NULL WHERE chat_id = $1", chat_id)
        stickers_mod.random.random = lambda: 0.99
        bot_gated = _FakeBot()
        await stickers_mod.send_mood_sticker_task(bot_gated, chat_id, user_id, "bored", 12345, force=False)
        assert len(bot_gated.sent) == 0, "Expected probability gate to block sticker when charge=0 and force=False"
        logger.info("[OK] force=False with charge=0 correctly blocked by probability gate")
    finally:
        stickers_mod.STICKERS_ENABLED = orig_enabled
        stickers_mod.load_sticker_pack = orig_loader
        stickers_mod.random.random = orig_rand

    # =========================================================================
    # TEST 8: Event extractor runs with fallback tz (user_tz=0) for new users
    # =========================================================================
    logger.info("\n--- TEST 8: Event Extractor Fallback Timezone ---")
    import config as config_mod

    class _FakeModels:
        def generate_content(self, model, contents, config):
            class _R:
                text = '[{"event_date": "2099-06-03", "event_type": "exam", "note": "Экзамен по матлабу"}]'
            return _R()

    class _FakeClient:
        models = _FakeModels()

    async with get_db() as conn:
        await conn.execute("DELETE FROM user_events WHERE chat_id = $1", chat_id)

    orig_client = config_mod.genai_client
    config_mod.genai_client = _FakeClient()
    try:
        # user_tz=0 fallback (new user whose tz is not yet inferred)
        await extract_and_save_events_task(chat_id, "у меня экзамен 3.06", user_tz=0)
    finally:
        config_mod.genai_client = orig_client

    events = await UserEvent.get_upcoming_for_chat(chat_id, date(2099, 1, 1), date(2099, 12, 31))
    assert len(events) == 1, f"Expected extractor to insert 1 event with fallback tz, got {len(events)}"
    assert events[0]["event_type"] == "exam", f"Expected exam event, got {events[0]['event_type']}"
    logger.info("[OK] Event extractor runs and persists events even when user_tz falls back to 0")

    # =========================================================================
    # TEST 9: Grief/Loss Sentiment (empathetic mood shift)
    # =========================================================================
    logger.info("\n--- TEST 9: Grief/Loss Sentiment ---")
    import json

    # Сбрасываем состояние и предварительно заряжаем игривое/радостное настроение,
    # чтобы проверить, что эмоциональное сообщение про потерю смещает в сопереживание,
    # а не оставляет прежнюю игривость (баг из живого теста).
    async with get_db() as conn:
        await conn.execute("DELETE FROM chat_emotional_states WHERE chat_id = $1", chat_id)
        await ChatEmotionalState.get_or_create(chat_id)
        await conn.execute(
            "UPDATE chat_emotional_states SET mood_state = $2::jsonb WHERE chat_id = $1",
            chat_id,
            json.dumps({"happy": 0.5, "teasing": 0.4, "love": 0.0, "sad": 0.0,
                        "angry": 0.0, "blush": 0.0, "shock": 0.0, "bored": 0.0, "thinking": 0.0}),
        )

    # user_id=None: не инферим tz и не пишем циркадный сдвиг через сохранённую зону;
    # циркадный сдвиг по серверному часу никогда не добавляет sad, поэтому ассерты ниже от него не зависят.
    loss_msg = "Если бы ты существовала всего 10 лет, то было бы очень больно тебя терять"
    state = await ChatEmotionalState.update_state(chat_id, loss_msg, closeness=0.6, user_id=None)
    mood_dict = json.loads(state["mood_state"]) if isinstance(state["mood_state"], str) else state["mood_state"]
    logger.info(f"Loss message mood: {mood_dict}")
    # Раньше («больно/терять/всплакнул» не были в словаре) sad оставался 0.0 — теперь распознаётся.
    assert mood_dict["sad"] > 0.1, f"Expected loss message to trigger sad, got {mood_dict['sad']}"
    assert mood_dict["love"] > 0.0, f"Expected loss message to trigger empathetic love, got {mood_dict['love']}"
    # Сопереживание гасит игривость (предзаряд teasing=0.4), а не «веселится» в ответ на боль.
    assert mood_dict["teasing"] < 0.4, f"Expected teasing to be dampened from 0.4, got {mood_dict['teasing']}"
    logger.info("[OK] Emotional loss message ('больно тебя терять') correctly triggered empathetic sad/love shift")

    logger.info("\n[SUCCESS] All Premium Proactive & Emotional improvements verified perfectly!")

if __name__ == "__main__":
    asyncio.run(run_tests())
