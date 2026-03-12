import os
import uuid
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional

import asyncpg
import chromadb
from chromadb.config import Settings

logger = logging.getLogger("MemoryManager")

class StateDB:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None
        self.db_url = os.getenv("DATABASE_URL")

    async def connect(self):
        if not self.db_url: raise ValueError("DATABASE_URL is missing!")
        try:
            self.pool = await asyncpg.create_pool(
                self.db_url,
                min_size=2,
                max_size=20,
                command_timeout=5.0
            )
            await self._create_tables()
            logger.info("✅ PostgreSQL (Tier 1 & 2): Connection pool ready.")
        except Exception as e:
            logger.critical(f"❌ StateDB Init Failed: {e}")
            raise

    async def _create_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_states (
                    uid BIGINT PRIMARY KEY,
                    active_skill TEXT DEFAULT 'logos',
                    is_dialogue INTEGER DEFAULT 0,
                    msg_count INTEGER DEFAULT 0,
                    subscription_end_date TIMESTAMPTZ,
                    bot_mode TEXT DEFAULT 'teacher',
                    is_incognito BOOLEAN DEFAULT FALSE,
                    manual_memory TEXT DEFAULT '',
                    last_interaction TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );

                -- МИГРАЦИЯ БАЗЫ: Добавляем трекинг экономики, если его нет
                ALTER TABLE user_states ADD COLUMN IF NOT EXISTS last_interaction TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;
                ALTER TABLE user_states ADD COLUMN IF NOT EXISTS tokens_used BIGINT DEFAULT 0;
                ALTER TABLE user_states ADD COLUMN IF NOT EXISTS tts_chars BIGINT DEFAULT 0;

                CREATE TABLE IF NOT EXISTS payments (
                    charge_id TEXT PRIMARY KEY,
                    uid BIGINT,
                    amount INTEGER,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS chat_history (
                    id SERIAL PRIMARY KEY,
                    uid BIGINT,
                    skill TEXT,
                    role TEXT,
                    content TEXT,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );

                CREATE INDEX IF NOT EXISTS idx_chat_history_uid_skill
                ON chat_history(uid, skill, id DESC);
            """)

    async def get_state(self, uid: int) -> Dict[str, Any]:
        if not self.pool: return {"active_skill": "logos", "is_dialogue": 0, "bot_mode": "teacher"}
        query = """
            INSERT INTO user_states (uid, active_skill)
            VALUES ($1, 'logos')
            ON CONFLICT (uid) DO UPDATE
            SET last_interaction = CURRENT_TIMESTAMP
            RETURNING *;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, uid)
            return dict(row) if row else {"active_skill": "logos"}

    async def update_state(self, uid: int, data: Dict[str, Any]):
        if not self.pool or not data: return
        ALLOWED_FIELDS = {"active_skill", "is_dialogue", "bot_mode", "is_incognito", "manual_memory", "subscription_end_date"}
        safe_data = {k: v for k, v in data.items() if k in ALLOWED_FIELDS}
        if not safe_data: return

        set_clauses = [f"{key} = ${i}" for i, key in enumerate(safe_data.keys(), start=2)]
        values = [uid] + list(safe_data.values())
        query = f"UPDATE user_states SET {', '.join(set_clauses)} WHERE uid = $1"

        async with self.pool.acquire() as conn:
            await conn.execute(query, *values)

    async def increment_msg_count(self, uid: int):
        if not self.pool: return
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE user_states SET msg_count = msg_count + 1, last_interaction = CURRENT_TIMESTAMP WHERE uid = $1", uid)

    async def update_economics(self, uid: int, tokens: int, tts_chars: int):
        """Обновление Unit-экономики пользователя"""
        if not self.pool or (tokens == 0 and tts_chars == 0): return
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE user_states 
                SET tokens_used = tokens_used + $2, tts_chars = tts_chars + $3 
                WHERE uid = $1
            """, uid, tokens, tts_chars)

    async def reset_msg_count(self, uid: int):
        if not self.pool: return
        async with self.pool.acquire() as conn:
            await conn.execute("UPDATE user_states SET msg_count = 0 WHERE uid = $1", uid)

    async def save_payment(self, uid: int, charge_id: str, amount: int) -> bool:
        if not self.pool: return False
        async with self.pool.acquire() as conn:
            res = await conn.fetchval("INSERT INTO payments (charge_id, uid, amount) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING RETURNING 1", charge_id, uid, amount)
            return bool(res)

    async def get_inactive_users(self, days: int = 3) -> List[int]:
        if not self.pool: return []
        target_time = datetime.now(timezone.utc) - timedelta(days=days)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("SELECT uid FROM user_states WHERE last_interaction < $1 AND last_interaction > $2", target_time, target_time - timedelta(days=1))
            return [r['uid'] for r in rows]

    async def disconnect(self):
        if self.pool: await self.pool.close()


class MemoryManager:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.state_db = StateDB()

        chroma_path = self.base_dir / "chroma_data"
        chroma_path.mkdir(exist_ok=True)
        self.chroma_client = chromadb.PersistentClient(
            path=str(chroma_path),
            settings=Settings(anonymized_telemetry=False)
        )
        self.long_term_collection = self.chroma_client.get_or_create_collection(name="long_term_memory")

    async def initialize(self):
        await self.state_db.connect()

    async def get_short_term_history(self, uid: int, skill: str, limit: int = 10) -> List[Dict[str, str]]:
        if not self.state_db.pool: return []
        async with self.state_db.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, content FROM chat_history WHERE uid = $1 AND skill = $2 ORDER BY id DESC LIMIT $3",
                uid, skill, limit
            )
            return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    async def save_interaction(self, uid: int, skill: str, user_text: str, ai_reply: str, is_incognito: bool):
        if not self.state_db.pool: return

        async with self.state_db.pool.acquire() as conn:
            await conn.execute("INSERT INTO chat_history (uid, skill, role, content) VALUES ($1, $2, 'user', $3)", uid, skill, user_text)
            await conn.execute("INSERT INTO chat_history (uid, skill, role, content) VALUES ($1, $2, 'assistant', $3)", uid, skill, ai_reply)

        if not is_incognito and "_dialogue" not in skill and "_translator" not in skill:
            doc_id = str(uuid.uuid4())
            fact_content = f"User said: {user_text}\nAI replied: {ai_reply}"

            def _save_to_chroma():
                try:
                    self.long_term_collection.add(documents=[fact_content], metadatas=[{"uid": str(uid), "skill": skill}], ids=[doc_id])
                except Exception as e:
                    logger.error(f"❌ ChromaDB Insert Error: {e}")

            asyncio.create_task(asyncio.to_thread(_save_to_chroma))

    async def build_context_prompt(self, uid: int, skill: str, text: str, state: Dict[str, Any]) -> str:
        context_parts = []
        if state.get("manual_memory"):
            context_parts.append(f"Факты о пользователе (Tier 1):\n{state['manual_memory']}")

        if not state.get("is_incognito", False) and text and "_dialogue" not in skill:
            def _query_chroma():
                try:
                    return self.long_term_collection.query(
                        query_texts=[text],
                        n_results=2,
                        where={"$and": [{"uid": {"$eq": str(uid)}}, {"skill": {"$eq": skill}}]}
                    )
                except Exception as e: return None

            res = await asyncio.to_thread(_query_chroma)
            if res and res.get('documents') and res['documents'][0]:
                context_parts.append("Исторический контекст (Tier 3 RAG):\n" + "\n".join(res['documents'][0]))

        return "\n\n".join(context_parts) if context_parts else ""
        
    async def wipe_all_user_data(self, uid: int):
        """RIGHT TO BE FORGOTTEN: Физическое удаление пользователя из всех слоев памяти."""
        # 1. Удаление из ChromaDB (в отдельном потоке, чтобы не блокировать Loop)
        def _wipe_chroma():
            try:
                self.long_term_collection.delete(where={"uid": {"$eq": str(uid)}})
            except Exception as e:
                logger.error(f"Chroma Wipe Error for uid {uid}: {e}")
        await asyncio.to_thread(_wipe_chroma)

        # 2. Удаление из PostgreSQL
        if self.state_db.pool:
            async with self.state_db.pool.acquire() as conn:
                await conn.execute("DELETE FROM chat_history WHERE uid = $1", uid)
                await conn.execute("DELETE FROM payments WHERE uid = $1", uid)
                await conn.execute("DELETE FROM user_states WHERE uid = $1", uid)
        
        logger.info(f"🗑 User {uid} has been completely wiped from the system.")

    async def shutdown(self):
        await self.state_db.disconnect()