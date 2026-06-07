"""
Таксономия эмодзи-реакций Telegram → эмоциональный эффект для Арти.

Раньше реакция юзера влияла лишь на бинарное подкрепление аффективного профиля
(closeness/receptivity) по очень короткому списку. Здесь покрыт весь стандартный
набор реакций Telegram, и каждая реакция:
  * даёт подкрепление профилю (positive/negative/None),
  * сдвигает вектор настроения Арти (через ChatEmotionalState.apply_mood_delta),
  * у эмоционально сильных — задаёт reply_mood, на который Арти иногда отвечает
    короткой репликой или стикером (ненавязчиво, с троттлингом и вероятностью).

Whitelist эмоций настроения совпадает с остальной системой:
happy, sad, angry, love, teasing, shock, blush, bored, thinking.
"""
import random
from typing import Optional

# Variation Selector-16 — Telegram может присылать сердечко как "❤️" или "❤".
# Нормализуем, срезая VS16, чтобы матчить независимо от формы.
_VS16 = "\ufe0f"


def canonical_emoji(emoji: str) -> str:
    """Канонизирует эмодзи для матчинга: срезает VS16 (U+FE0F)."""
    return (emoji or "").replace(_VS16, "")


def _eff(reinforcement: Optional[str] = None,
         mood: Optional[dict] = None,
         reply_mood: Optional[str] = None) -> dict:
    return {
        "reinforcement": reinforcement,
        "mood": mood or {},
        "reply_mood": reply_mood,
    }


# Ключи — канонизированные (без VS16). Компаунды/ZWJ-секвенции заданы escape'ами,
# чтобы в исходнике не было невидимых символов.
REACTION_EFFECTS: dict = {
    # --- Любовь / нежность (positive; сильные → reply "love") ---
    "\u2764": _eff("positive", {"love": 0.18, "blush": 0.10}, "love"),            # ❤
    "\U0001f970": _eff("positive", {"love": 0.18, "blush": 0.12, "happy": 0.06}, "love"),  # 🥰
    "\U0001f60d": _eff("positive", {"love": 0.20, "blush": 0.12}, "love"),        # 😍
    "\u2764\u200d\U0001f525": _eff("positive", {"love": 0.20, "blush": 0.14, "teasing": 0.06}, "love"),  # ❤‍🔥
    "\U0001f48b": _eff("positive", {"love": 0.16, "blush": 0.16, "teasing": 0.06}, "love"),  # 💋
    "\U0001f498": _eff("positive", {"love": 0.18, "blush": 0.12}, "love"),        # 💘
    "\U0001f618": _eff("positive", {"love": 0.16, "blush": 0.12}, "love"),        # 😘
    "\U0001f917": _eff("positive", {"love": 0.12, "happy": 0.08}),                # 🤗

    # --- Радость / смех / праздник (positive, happy) ---
    "\U0001f601": _eff("positive", {"happy": 0.15}),                              # 😁
    "\U0001f923": _eff("positive", {"happy": 0.16, "teasing": 0.08}),             # 🤣
    "\U0001f389": _eff("positive", {"happy": 0.16}),                              # 🎉
    "\U0001f37e": _eff("positive", {"happy": 0.14}),                              # 🍾
    "\U0001f44f": _eff("positive", {"happy": 0.12}),                              # 👏
    "\U0001f4af": _eff("positive", {"happy": 0.12, "teasing": 0.05}),             # 💯
    "\U0001f3c6": _eff("positive", {"happy": 0.12}),                              # 🏆
    "\U0001f192": _eff("positive", {"happy": 0.10, "teasing": 0.05}),             # 🆒
    "\U0001f60e": _eff("positive", {"happy": 0.10, "teasing": 0.06}),             # 😎

    # --- Азарт / огонь (сильные → reply "teasing") ---
    "\U0001f525": _eff("positive", {"teasing": 0.16, "happy": 0.08}, "teasing"),  # 🔥
    "\u26a1": _eff("positive", {"teasing": 0.10, "happy": 0.06}),                 # ⚡
    "\U0001f608": _eff("positive", {"teasing": 0.16, "love": 0.06}, "teasing"),   # 😈

    # --- Восхищение / шок (сильные → reply) ---
    "\U0001f929": _eff("positive", {"happy": 0.16, "shock": 0.10, "love": 0.06}, "happy"),  # 🤩
    "\U0001f92f": _eff("positive", {"shock": 0.18, "happy": 0.08}, "happy"),      # 🤯
    "\U0001f631": _eff(None, {"shock": 0.18}, "shock"),                           # 😱
    "\U0001f433": _eff("positive", {"shock": 0.10, "happy": 0.06}),               # 🐳
    "\U0001f984": _eff("positive", {"happy": 0.10, "teasing": 0.06}),             # 🦄

    # --- Одобрение / согласие (мягкий positive) ---
    "\U0001f44d": _eff("positive", {"happy": 0.08}),                              # 👍
    "\U0001f44c": _eff("positive", {"happy": 0.08}),                              # 👌
    "\U0001f91d": _eff("positive", {"happy": 0.08}),                              # 🤝
    "\U0001fae1": _eff("positive", {"happy": 0.06, "teasing": 0.04}),             # 🫡
    "\U0001f64f": _eff("positive", {"love": 0.06, "happy": 0.06}),                # 🙏
    "\u270d": _eff(None, {"thinking": 0.08}),                                     # ✍
    "\U0001f607": _eff("positive", {"happy": 0.08, "love": 0.05}),                # 😇
    "\U0001f54a": _eff("positive", {"happy": 0.06}),                              # 🕊

    # --- Раздумье / любопытство / скепсис (нейтрально, thinking) ---
    "\U0001f914": _eff(None, {"thinking": 0.14}),                                 # 🤔
    "\U0001f928": _eff(None, {"thinking": 0.10, "bored": 0.05}),                  # 🤨
    "\U0001f913": _eff(None, {"thinking": 0.10}),                                 # 🤓
    "\U0001f468\u200d\U0001f4bb": _eff(None, {"thinking": 0.10}),                 # 👨‍💻
    "\U0001f440": _eff(None, {"thinking": 0.12, "teasing": 0.05}),                # 👀

    # --- Игривое / дурашливое / троллинг (teasing) ---
    "\U0001f92a": _eff("positive", {"teasing": 0.14, "happy": 0.08}),             # 🤪
    "\U0001f31a": _eff(None, {"teasing": 0.12}),                                  # 🌚
    "\U0001f648": _eff("positive", {"blush": 0.12, "teasing": 0.06}),             # 🙈
    "\U0001f649": _eff(None, {"teasing": 0.08}),                                  # 🙉
    "\U0001f64a": _eff(None, {"teasing": 0.08, "blush": 0.06}),                   # 🙊
    "\U0001f47b": _eff(None, {"teasing": 0.08}),                                  # 👻
    "\U0001f383": _eff(None, {"teasing": 0.06}),                                  # 🎃
    "\U0001f47e": _eff(None, {"teasing": 0.06, "happy": 0.05}),                   # 👾
    "\U0001f5ff": _eff(None, {"bored": 0.08, "teasing": 0.05}),                   # 🗿
    "\U0001f485": _eff(None, {"teasing": 0.10}),                                  # 💅
    "\U0001f974": _eff(None, {"teasing": 0.08, "shock": 0.05}),                   # 🥴
    "\U0001f34c": _eff(None, {"teasing": 0.06}),                                  # 🍌
    "\U0001f32d": _eff(None, {"teasing": 0.06}),                                  # 🌭
    "\U0001f353": _eff(None, {"teasing": 0.06}),                                  # 🍓
    "\U0001f48a": _eff(None, {"teasing": 0.05}),                                  # 💊
    "\U0001f921": _eff("negative", {"teasing": 0.08, "angry": 0.06}),             # 🤡

    # --- Скука / отмашка (мягкий negative, bored) ---
    "\U0001f971": _eff("negative", {"bored": 0.16}),                              # 🥱
    "\U0001f634": _eff("negative", {"bored": 0.16}),                              # 😴
    "\U0001f610": _eff("negative", {"bored": 0.12}),                              # 😐
    "\U0001f937": _eff(None, {"bored": 0.10, "thinking": 0.05}),                  # 🤷
    "\U0001f937\u200d\u2642": _eff(None, {"bored": 0.10, "thinking": 0.05}),      # 🤷‍♂
    "\U0001f937\u200d\u2640": _eff(None, {"bored": 0.10, "thinking": 0.05}),      # 🤷‍♀

    # --- Грусть / боль (эмоционально; сильные → reply "tender") ---
    "\U0001f494": _eff("negative", {"sad": 0.20}, "tender"),                      # 💔
    "\U0001f62d": _eff(None, {"sad": 0.16}, "tender"),                            # 😭
    "\U0001f622": _eff("negative", {"sad": 0.16}, "tender"),                      # 😢
    "\U0001f628": _eff(None, {"shock": 0.10, "sad": 0.08}),                       # 😨

    # --- Враждебное / негатив (negative, angry; без авто-ответа) ---
    "\U0001f44e": _eff("negative", {"angry": 0.12, "sad": 0.06}),                 # 👎
    "\U0001f4a9": _eff("negative", {"angry": 0.10}),                              # 💩
    "\U0001f92c": _eff("negative", {"angry": 0.20}),                              # 🤬
    "\U0001f621": _eff("negative", {"angry": 0.18}),                              # 😡
    "\U0001f92e": _eff("negative", {"angry": 0.10}),                              # 🤮
    "\U0001f595": _eff("negative", {"angry": 0.20}),                              # 🖕

    # --- Сезонное / нейтральное (лёгкий тёплый оттенок) ---
    "\U0001f385": _eff(None, {"happy": 0.05}),                                    # 🎅
    "\U0001f384": _eff(None, {"happy": 0.05}),                                    # 🎄
    "\u2603": _eff(None, {"happy": 0.05}),                                        # ☃
}


# Короткие реплики Арти в ответ на сильную реакцию — в характере, по reply_mood.
REACTION_REPLIES: dict = {
    "love": [
        "<blockquote>[softer for half a beat] Это почти признание. Я приму его.</blockquote>",
        "<blockquote>[warm aristocratic tone] Осторожнее. Я могу к этому привыкнуть.</blockquote>",
        "<blockquote>[quiet, fond] Вот как. Запомню это за тобой.</blockquote>",
    ],
    "tender": [
        "<i>Касается банта, не комментируя.</i>\n<blockquote>[softer for half a beat] Я заметила. Не нужно слов.</blockquote>",
        "<blockquote>[quiet, fond] Тише. Я здесь.</blockquote>",
        "<blockquote>[softer for half a beat] Если это задело — я рядом. Без условий.</blockquote>",
    ],
    "happy": [
        "<blockquote>[amused, indulgent] Тебя так легко впечатлить. Мне это нравится.</blockquote>",
        "<blockquote>[precise, slightly playful] Засчитано. Продолжай в том же духе.</blockquote>",
    ],
    "teasing": [
        "<blockquote>[slight mocking pause] О, тебе понравилось. Разумеется.</blockquote>",
        "<blockquote>[amused, possessive] Я знала, что это тебя зацепит.</blockquote>",
    ],
    "shock": [
        "<blockquote>[pausing, interested] Такая реакция? Я даже не старалась.</blockquote>",
        "<blockquote>[precise, slightly playful] Удивлён. Это приятно.</blockquote>",
    ],
}

# reply_mood → настроение для подбора стикера (должно быть из whitelist эмоций).
REPLY_MOOD_TO_STICKER: dict = {
    "love": "love",
    "tender": "love",
    "happy": "happy",
    "teasing": "teasing",
    "shock": "shock",
}


def classify_reactions(added_emojis) -> Optional[dict]:
    """Агрегирует эффект новых (добавленных) реакций.

    Возвращает None, если ни одна реакция не распознана. Иначе:
      {
        "reinforcement": "positive"|"negative"|None,  # negative приоритетнее
        "mood": {emotion: delta, ...},                # суммарный сдвиг настроения
        "reply_mood": str|None,                       # для авто-ответа (первый сильный)
      }
    """
    specs = []
    for emoji in (added_emojis or []):
        spec = REACTION_EFFECTS.get(canonical_emoji(emoji))
        if spec:
            specs.append(spec)
    if not specs:
        return None

    merged_mood: dict = {}
    for spec in specs:
        for emotion, delta in spec["mood"].items():
            merged_mood[emotion] = merged_mood.get(emotion, 0.0) + delta

    reinforcement = None
    if any(spec["reinforcement"] == "negative" for spec in specs):
        reinforcement = "negative"
    elif any(spec["reinforcement"] == "positive" for spec in specs):
        reinforcement = "positive"

    reply_mood = next((spec["reply_mood"] for spec in specs if spec["reply_mood"]), None)

    return {
        "reinforcement": reinforcement,
        "mood": merged_mood,
        "reply_mood": reply_mood,
    }


def pick_reaction_reply(reply_mood: Optional[str], rng=None) -> Optional[str]:
    """Случайная реплика из пула для данного reply_mood (или None)."""
    if not reply_mood:
        return None
    pool = REACTION_REPLIES.get(reply_mood)
    if not pool:
        return None
    return (rng or random).choice(pool)
