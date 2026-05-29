import argparse
import asyncio
import json
import sys

from database.connection import close_db, init_db
from memory.consolidator import consolidate_chat_facts
from memory.profiles import refresh_user_profile
from memory.timeline import build_timeline_events


async def run(chat_id: int, mode: str, limit: int, apply: bool, profile: bool, timeline: bool, user_id: int = None):
    await init_db()
    try:
        reports = {}
        if not profile and not timeline:
            reports["consolidation"] = await consolidate_chat_facts(
                chat_id=chat_id,
                mode=mode,
                limit=limit,
                dry_run=not apply,
            )
        if profile:
            reports["profile"] = await refresh_user_profile(
                chat_id=chat_id,
                user_id=user_id or chat_id,
                mode=mode,
                dry_run=not apply,
            )
        if timeline:
            reports["timeline"] = await build_timeline_events(
                chat_id=chat_id,
                mode=mode,
                limit=limit,
                dry_run=not apply,
            )
        print(json.dumps(reports, ensure_ascii=False, indent=2, default=str))
    finally:
        await close_db()


def parse_args():
    parser = argparse.ArgumentParser(description="Memory maintenance for Arti long-term memory")
    parser.add_argument("--chat-id", type=int, required=True)
    parser.add_argument("--mode", default="default")
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--user-id", type=int)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--timeline", action="store_true")
    parser.add_argument("--apply", action="store_true", help="Apply maintenance changes. Default is dry-run.")
    return parser.parse_args()


if __name__ == "__main__":
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    args = parse_args()
    asyncio.run(run(args.chat_id, args.mode, args.limit, args.apply, args.profile, args.timeline, args.user_id))
