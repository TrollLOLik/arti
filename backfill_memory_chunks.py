import argparse
import asyncio
import logging
import sys

from database.connection import close_db, init_db
from database.models import MemoryChunk, MemoryMessage
from memory.chunking import build_compact_chunks
from memory.embeddings import EMBEDDING_MODEL, embed_document

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def backfill(batch_size: int, max_messages: int, embed_limit: int, sleep_seconds: float):
    await init_db()
    processed_messages = 0
    created_chunks = 0
    embedded_chunks = 0
    after_id = 0

    try:
        while True:
            if max_messages and processed_messages >= max_messages:
                break

            current_limit = batch_size
            if max_messages:
                current_limit = min(current_limit, max_messages - processed_messages)

            messages = await MemoryMessage.fetch_for_chunking(limit=current_limit, after_id=after_id)
            if not messages:
                break

            after_id = max(int(message["id"]) for message in messages)
            processed_messages += len(messages)
            chunks = build_compact_chunks(messages)

            for chunk in chunks:
                chunk_id = await MemoryChunk.create(**chunk)
                if chunk_id:
                    created_chunks += 1

            logger.info(
                "Chunk batch processed: messages=%s, chunks_total=%s, last_message_id=%s",
                processed_messages,
                created_chunks,
                after_id,
            )

        while True:
            if embed_limit and embedded_chunks >= embed_limit:
                break

            limit = 25
            if embed_limit:
                limit = min(limit, embed_limit - embedded_chunks)

            chunks = await MemoryChunk.get_unembedded(limit=limit)
            if not chunks:
                break

            for chunk in chunks:
                vector = await embed_document(chunk["chunk_text"])
                if not vector:
                    logger.warning("Embedding пустой для chunk_id=%s", chunk["id"])
                    continue
                await MemoryChunk.set_embedding(chunk["id"], vector, EMBEDDING_MODEL)
                embedded_chunks += 1
                if sleep_seconds:
                    await asyncio.sleep(sleep_seconds)

            logger.info("Embedding batch processed: embedded_total=%s", embedded_chunks)

        logger.info(
            "Backfill complete: processed_messages=%s, created_or_existing_chunks=%s, embedded_chunks=%s",
            processed_messages,
            created_chunks,
            embedded_chunks,
        )
    finally:
        await close_db()


def parse_args():
    parser = argparse.ArgumentParser(description="Backfill memory_chunks and Gemini embeddings for Arti memory RAG")
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--max-messages", type=int, default=0)
    parser.add_argument("--embed-limit", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0.2)
    return parser.parse_args()


if __name__ == "__main__":
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    args = parse_args()
    asyncio.run(backfill(args.batch_size, args.max_messages, args.embed_limit, args.sleep))
