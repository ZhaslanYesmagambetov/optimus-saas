import os
import uuid
import logging
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Dict, Any, Optional

import asyncpg
import chromadb

logger = logging.getLogger("MemoryManager")

class StateDB:
    def __init__(self):
        self.pool = None
        self.db_url = os.getenv("DATABASE_URL")

    async def connect(self):
        if not self.db_url: raise ValueError("DATABASE_URL не найден!")
        try:
            self.pool = await asyncpg.create_pool(self.db_url, min_size=1, max_size=20)
            await self._create_tables()
            logger.info("✅ PostgreSQL (StateDB): Пул соединений готов.")
        except Exception as e:
            logger.error(f"❌ Ошибка БД: {e}")
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
                
                ALTER TABLE user_states ADD COLUMN IF NOT EXISTS last_interaction TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;
                
                CREATE TABLE IF NOT EXISTS payments (
                    charge_id TEXT PRIMARY KEY,
                    uid BIGINT,
                    amount INTEGER,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
            """)

    async def get_state(self, uid: int) -> Dict[str, Any]:
        if not self.pool: return {"active_skill": "logos", "is_dialogue": 0, "bot_mode": "teacher"}
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM user_states WHERE uid = $1", uid)
            if not row:
                await conn.execute("INSERT INTO user_states (uid, active_skill) VALUES ($1, 'logos') ON CONFLICT DO NOTHING", uid)
                row = await conn.fetchrow("SELECT * FROM user_states WHERE uid = $1", uid)
            return dict(row) if row else {"active_skill": "logos", "msg_count": 0}

    async def update_state(self, uid: int, data: Dict[str, Any]):
        """ОПТИМИЗАЦИЯ КЛОДА: Атомарный UPDATE без лишнего SELECT"""
        if not self.pool or not data: return
        ALLOWED_FIELDS = {"active_skill", "is_dialogue", "msg_count", "subscription_end_date", "bot_mode", "is_incognito", "manual_memory", "last_interaction"}
        safe_data = {k: v for k, v in data.items() if k in ALLOWED_FIELDS}
        if not safe_data: return
        
        set_clauses = []
        values = [uid]
        for i, (key, value) in enumerate(safe_data.items(), start=2):
            set_clauses.append(f"{key} = ${i}")
            values.append(value)
            
        query = f"UPDATE user_states SET {', '.join(set_clauses)} WHERE uid = $1"
        
        async with self.pool.acquire() as conn:
            res = await conn.execute(query, *values)
            # Если пользователя нет в базе (UPDATE 0), создаем его и повторяем UPDATE
            if res == "UPDATE 0":
                await conn.execute("INSERT INTO user_states (uid) VALUES ($1) ON CONFLICT DO NOTHING", uid)
                await conn.execute(query, *values)

    async def increment_msg_count(self, uid: int):
        if not self.pool: return
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO user_states (uid, msg_count, last_interaction) 
                VALUES ($1, 1, CURRENT_TIMESTAMP)
                ON CONFLICT (uid) 
                DO UPDATE SET msg_count = user_states.msg_count + 1, last_interaction = CURRENT_TIMESTAMP;
            """, uid)

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
            rows = await conn.fetch("""
                SELECT uid FROM user_states 
                WHERE last_interaction < $1 AND last_interaction > $2
            """, target_time, target_time - timedelta(days=1))
            return [r['uid'] for r in rows]

    async def disconnect(self):
        if self.pool: await self.pool.close()

class ShortTermDB:
    def __init__(self, pool):
        self.get_pool = lambda: pool 

    async def _create_tables(self):
        pool = self.get_pool()
        if not pool: return
        async with pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_history (
                    id SERIAL PRIMARY KEY,
                    uid BIGINT,
                    skill TEXT,
                    role TEXT,
                    content TEXT,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_chat_history_uid_skill_time 
                ON chat_history(uid, skill, created_at DESC);
            """)

    async def save_message(self, uid: int, skill: str, role: str, content: str):
        pool = self.get_pool()
        if not pool: return
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO chat_history (uid, skill, role, content) VALUES ($1, $2, $3, $4)", uid, skill, role, content)
        asyncio.create_task(self._cleanup_history(uid, skill))

    async def _cleanup_history(self, uid: int, skill: str):
        pool = self.get_pool()
        if not pool: return
        try:
            async with pool.acquire() as conn:
                await conn.execute("""
                    DELETE FROM chat_history WHERE uid = $1 AND skill = $2 AND id <= (
                        SELECT id FROM chat_history WHERE uid = $1 AND skill = $2 ORDER BY id DESC OFFSET 100 LIMIT 1
                    )
                """, uid, skill)
        except Exception: pass

    async def get_chat_history(self, uid: int, skill: str, limit: int = 10) -> List[Dict[str, str]]:
        pool = self.get_pool()
        if not pool: return []
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT role, content FROM chat_history WHERE uid = $1 AND skill = $2 ORDER BY id DESC LIMIT $3", uid, skill, limit)
            return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    async def get_history_for_period(self, skill: str, days: int = 7) -> Dict[int, str]:
        pool = self.get_pool()
        if not pool: return {}
        time_limit = datetime.now(timezone.utc) - timedelta(days=days)
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT uid, role, content FROM chat_history 
                WHERE skill = $1 AND created_at >= $2 
                ORDER BY uid, id ASC
            """, skill, time_limit)
            
            user_dialogs = {}
            for r in rows:
                uid, role, content = r['uid'], r['role'], r['content']
                if uid not in user_dialogs: user_dialogs[uid] = ""
                user_dialogs[uid] += f"[{role.upper()}]: {content}\n"
            return user_dialogs

    async def clear_history(self, uid: int, skill: str):
        pool = self.get_pool()
        if pool:
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM chat_history WHERE uid = $1 AND skill = $2", uid, skill)

class MemoryManager:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.state_db = StateDB()
        self.short_term = None 
        
        chroma_path = self.base_dir / "chroma_data"
        chroma_path.mkdir(exist_ok=True)
        self.chroma_client = chromadb.PersistentClient(path=str(chroma_path))
        self.long_term_collection = self.chroma_client.get_or_create_collection(name="long_term_memory")

    async def initialize(self):
        await self.state_db.connect()
        self.short_term = ShortTermDB(self.state_db.pool)
        if self.state_db.pool: await self.short_term._create_tables()

    async def process_interaction(self, uid: int, skill: str, user_text: str, ai_reply: str):
        await self.state_db.increment_msg_count(uid)
        state = await self.state_db.get_state(uid)
        if state.get("is_incognito", False): return
        
        if self.short_term:
            await self.short_term.save_message(uid, skill, "user", user_text)
            await self.short_term.save_message(uid, skill, "assistant", ai_reply)

        if "_dialogue" in skill or "_translator" in skill: return

        doc_id = str(uuid.uuid4())
        fact_content = f"User said: {user_text}\nAI replied: {ai_reply}"
        def _save_to_chroma():
            try:
                self.long_term_collection.add(
                    documents=[fact_content], metadatas=[{"uid": str(uid), "skill": skill}], ids=[doc_id]
                )
            except Exception: pass
        await asyncio.to_thread(_save_to_chroma)

    async def build_context_prompt(self, uid: int, skill: str, text: str) -> str:
        state = await self.state_db.get_state(uid)
        context_parts = []
        
        if state.get("manual_memory"):
            context_parts.append(f"Факты о пользователе:\n{state['manual_memory']}")
            
        if not state.get("is_incognito", False) and text and "_dialogue" not in skill and "_translator" not in skill:
            def _query_chroma():
                # ОПТИМИЗАЦИЯ КЛОДА: Прямой синтаксис ChromaDB без $and
                try: return self.long_term_collection.query(query_texts=[text], n_results=3, where={"uid": str(uid), "skill": skill})
                except Exception: return None

            res = await asyncio.to_thread(_query_chroma)
            if res and res.get('documents') and res['documents'][0]:
                context_parts.append("Предыдущий контекст RAG:\n" + "\n".join(res['documents'][0]))
                
        return "\n\n".join(context_parts) if context_parts else ""

    async def shutdown(self):
        await self.state_db.disconnect()