import os
import asyncio
import logging
import uuid
import re
import json
import io
import time
import random
from pathlib import Path
from dotenv import load_dotenv
from typing import List, Optional, Dict, Any, Set
from datetime import datetime, timedelta, timezone, time as dt_time
from cachetools import TTLCache

import azure.cognitiveservices.speech as speechsdk
from google import genai
from google.genai import types
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, BotCommand, LabeledPrice
from telegram.constants import ParseMode, ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, PreCheckoutQueryHandler, filters, ContextTypes, Application

from memory_manager import MemoryManager

# === CONFIGURATION & OBSERVABILITY ===
BASE_DIR = Path(__file__).parent
load_dotenv(dotenv_path=BASE_DIR / '.env')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
AZURE_SPEECH_KEY = os.getenv("AZURE_SPEECH_KEY")
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION")

FREE_MESSAGE_LIMIT = 50
PRO_PRICE_STARS = 250
TZ_ASTANA = timezone(timedelta(hours=5)) 

try: 
    ROOT_ADMINS = [int(u) for u in os.getenv("ALLOWED_USERS", "").split(",") if u.strip()]
except ValueError: 
    ROOT_ADMINS = []

WHITELIST_FILE = BASE_DIR / "whitelist.json"

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger("OptimusSaaS.Core")

client = genai.Client(api_key=GOOGLE_API_KEY)
memory_manager = MemoryManager(BASE_DIR)
MODEL_NAME = "gemini-2.0-flash"

llm_semaphore = asyncio.Semaphore(20)
user_rate_limit = TTLCache(maxsize=10000, ttl=1.5) 

# === DOMAIN MAPPING ===
SKILL_MAPPING = {
    "english": "🇬🇧 Английский", "kazakh": "🇰🇿 Казахский", "python": "🐍 Python", "sysadmin": "💻 Сисадмин",
    "network": "🌐 Сети (Cisco)", "fitness": "💪 Фитнес", "nutrition": "🥗 Нутрициолог", "health_consultant": "🏥 Мед. Консультант",
    "psychologist": "🧠 Психолог", "career": "💼 Карьера", "finance": "💵 Финансы", "payroll": "💸 Бухгалтер-расчётчик",
    "accountant": "💰 Главный бухгалтер", "lawyer": "⚖️ Юрист", "marketing": "📈 Маркетинг", "fishing": "🎣 Дерсу Узала",
    "math": "🧮 Математика", "study_helper": "🎓 Учеба", "travel": "✈️ Путешествия", "family": "👨‍👩‍👧 Семья",
    "logos": "🏛 Логос", "socrates": "🤔 Сократ", "albert": "🔬 Альберт", "neuron": "🤖 Нейрон",
    "bobby": "💼 Бобби", "don_juan": "🌵 Дон Хуан"
}
UI_TO_SKILL = {v: k for k, v in SKILL_MAPPING.items()}

VOICE_MAP = {
    "kazakh": "kk-KZ-DauletNeural",
    "english": "en-US-ChristopherNeural", 
    "russian": "ru-RU-DmitryNeural",
    "russian_male": "ru-RU-DmitryNeural",
    "russian_female": "ru-RU-SvetlanaNeural",
    "russian_soft": "ru-RU-DariyaNeural"
}

SKILL_VOICES = {
    "psychologist": VOICE_MAP["russian_female"],
    "fishing": VOICE_MAP["russian_male"],
    "family": VOICE_MAP["russian_soft"],
    "career": VOICE_MAP["russian_male"],
    "logos": VOICE_MAP["russian_male"],
    "socrates": VOICE_MAP["russian_male"],
    "albert": VOICE_MAP["russian_male"],
    "neuron": VOICE_MAP["russian_soft"],
    "don_juan": VOICE_MAP["russian_male"],
    "bobby": VOICE_MAP["russian_male"],
    "lawyer": VOICE_MAP["russian_male"],
    "finance": VOICE_MAP["russian_male"],
    "accountant": VOICE_MAP["russian_male"],
    "payroll": VOICE_MAP["russian_male"],
    "marketing": VOICE_MAP["russian_male"],
    "health_consultant": VOICE_MAP["russian_soft"],
    "nutrition": VOICE_MAP["russian_soft"],
    "fitness": VOICE_MAP["russian_soft"],
    "travel": VOICE_MAP["russian_soft"],
    "study_helper": VOICE_MAP["russian_soft"],
    "kazakh": VOICE_MAP["russian_female"]
}

NO_VOICE_TEACHER_SKILLS = {"python", "sysadmin", "network", "math", "english", "kazakh"}

HELP_TEXT = """
🤖 **Optimus — Многофункциональный AI-Ассистент**

📚 **Навыки:**
Выбери навык из меню — Языки, IT, Здоровье, Финансы, Философия и другие.
Каждый навык — отдельный эксперт со своим характером и специализацией.

🧠 **Умная память:**
Бот обладает долгосрочной памятью! Он запоминает важные факты из ваших диалогов. Вы также можете задать жесткие факты о себе через меню 📊 Дашборд -> 📝 Редактировать профиль.

🎙 **Голосовой режим:**
Отправь голосовое сообщение — бот распознает речь и ответит текстом + аудио.

📸 **Анализ изображений и файлов:**
Отправь фото, PDF, Word или Excel с подписью — бот проанализирует содержимое.

🌐 **Переводчик:**
Выбери направление перевода:
🇷🇺➡️🇬🇧 RU→EN / 🇬🇧➡️🇷🇺 EN→RU
🇷🇺➡️🇰🇿 RU→KZ / 🇰🇿➡️🇷🇺 KZ→RU
Текст будет переведён и озвучен носителем языка.

🎙 **Режим диалога:**
Живой разговор — бот отвечает быстрее и короче. Отличная тренировка языка.

🧘 **Приложения:**
🧘 Дыхание — ZenBreath (дыхательные практики)
❄️ Вим Хоф — метод Вима Хофа

📊 **Дашборд:**
Сброс контекста, Инкогнито, управление памятью, статус подписки.

🕵️ **Инкогнито:**
Режим без сохранения истории.

**Команды:** /start — Вызов главного меню
/status — Дашборд и настройки
"""

class PromptManager:
    def __init__(self, base_dir: Path):
        self.prompts_dir = base_dir / "prompts"
        
    def get_teacher_prompt(self, skill: str) -> str:
        filepath = self.prompts_dir / f"{skill}.txt"
        if filepath.exists(): return filepath.read_text(encoding='utf-8').strip()
        return "SYSTEM: You are a helpful AI Assistant."
        
    def get_translator_prompt(self, target_lang: str) -> str:
        filepath = self.prompts_dir / "translate.txt"
        if filepath.exists(): return filepath.read_text(encoding='utf-8').strip().replace("{TARGET_LANGUAGE}", target_lang.upper())
        return f"SYSTEM: Translate strictly to {target_lang.upper()}."
        
    def get_dialogue_prompt(self, skill: str) -> str:
        filepath = self.prompts_dir / "dialogue.txt"
        if filepath.exists(): return filepath.read_text(encoding='utf-8').strip().replace("{LANGUAGE}", skill.upper())
        return "SYSTEM: Act as a conversational partner."

prompt_manager = PromptManager(BASE_DIR)

class WhitelistManager:
    def __init__(self):
        self.dynamic_users: Set[int] = set()
        self.load()
        
    def load(self):
        if WHITELIST_FILE.exists():
            try: self.dynamic_users = set(json.loads(WHITELIST_FILE.read_text()).get("users", []))
            except Exception: pass
                
    def save(self):
        WHITELIST_FILE.write_text(json.dumps({"users": list(self.dynamic_users)}))
        
    def is_admin(self, uid: int) -> bool:
        return (uid in ROOT_ADMINS) or (uid in self.dynamic_users)
        
    def add_user(self, uid: int):
        self.dynamic_users.add(uid)
        self.save()
        
    def remove_user(self, uid: int):
        if uid in self.dynamic_users: 
            self.dynamic_users.remove(uid)
            self.save()

whitelist = WhitelistManager()

async def _get_history_for_period(skill: str, days: int = 7) -> Dict[int, str]:
    if not memory_manager.state_db.pool: return {}
    time_limit = datetime.now(timezone.utc) - timedelta(days=days)
    async with memory_manager.state_db.pool.acquire() as conn:
        rows = await conn.fetch("SELECT uid, role, content FROM chat_history WHERE skill = $1 AND created_at >= $2 ORDER BY uid, id ASC", skill, time_limit)
        user_dialogs = {}
        for r in rows:
            u, role, content = r['uid'], r['role'], r['content']
            if u not in user_dialogs: user_dialogs[u] = ""
            user_dialogs[u] += f"[{role.upper()}]: {content}\n"
        return user_dialogs

async def cron_weekly_english(context: ContextTypes.DEFAULT_TYPE):
    dialogs = await _get_history_for_period("english", days=7)
    sys_prompt = "Ты AI-учитель английского. Сделай короткое ревью ошибок за неделю на русском и задай 2 вопроса."
    for uid, history_text in dialogs.items():
        if len(history_text) < 50: continue 
        try:
            resp = await asyncio.to_thread(
                client.models.generate_content, model=MODEL_NAME, contents=history_text, 
                config=types.GenerateContentConfig(temperature=0.7, system_instruction=sys_prompt)
            )
            await context.bot.send_message(chat_id=uid, text=f"🌟 **Еженедельный отчет (Английский)**\n\n{resp.text}", parse_mode=ParseMode.MARKDOWN)
            await asyncio.sleep(2) 
        except Exception: pass

async def cron_weekly_kazakh(context: ContextTypes.DEFAULT_TYPE):
    dialogs = await _get_history_for_period("kazakh", days=7)
    for uid in dialogs.keys():
        try:
            await context.bot.send_message(chat_id=uid, text="🇰🇿 Привет! Продолжим практиковать казахский язык?")
            await asyncio.sleep(1)
        except Exception: pass

async def cron_daily_ping(context: ContextTypes.DEFAULT_TYPE):
    inactive_uids = await memory_manager.state_db.get_inactive_users(days=3)
    for uid in inactive_uids:
        try:
            await context.bot.send_message(chat_id=uid, text="👋 Привет! Я всегда здесь, если захочешь попрактиковаться!")
            await asyncio.sleep(1)
        except Exception: pass

def clean_text_for_tts(text: str) -> str:
    text = re.sub(r'^(Аударма|Перевод|Translation)[:!]\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'[\*\_`#]', '', text)
    return re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text).strip()

async def generate_voice_bytes(text: str, voice: str) -> Optional[bytes]:
    clean_text = clean_text_for_tts(text)
    if not clean_text or len(clean_text) < 2 or not AZURE_SPEECH_KEY: 
        return None
        
    def _tts():
        try:
            cfg = speechsdk.SpeechConfig(subscription=AZURE_SPEECH_KEY, region=AZURE_SPEECH_REGION)
            cfg.speech_synthesis_voice_name = voice
            cfg.set_speech_synthesis_output_format(speechsdk.SpeechSynthesisOutputFormat.Audio16Khz32KBitRateMonoMp3)
            cfg.set_property(speechsdk.PropertyId.SpeechServiceConnection_InitialSilenceTimeoutMs, "15000")
            
            synthesizer = speechsdk.SpeechSynthesizer(speech_config=cfg, audio_config=None)
            
            if "kk-KZ" in voice:
                ssml = f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="kk-KZ"><voice name="{voice}">{clean_text}</voice></speak>'
                res = synthesizer.speak_ssml_async(ssml).get()
            else:
                res = synthesizer.speak_text_async(clean_text).get()
                
            if res.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                return res.audio_data
            else:
                return None
        except Exception:
            return None
            
    return await asyncio.to_thread(_tts)

async def safe_send(update: Update, text: str, use_markdown: bool = True):
    MAX = 3500
    for i in range(0, len(text), MAX):
        chunk = text[i:i+MAX]
        try: 
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN if use_markdown else None)
        except Exception: 
            await update.message.reply_text(chunk)

async def transcribe_audio_secure(file_id: str, context: ContextTypes.DEFAULT_TYPE, uid: int, skill: str, bot_mode: str) -> str:
    try:
        file = await context.bot.get_file(file_id)
        if not whitelist.is_admin(uid) and file.file_size and file.file_size > 10 * 1024 * 1024:
            return "⛔️ Голосовое сообщение слишком большое (максимум 10 МБ)."
            
        byte_stream = io.BytesIO()
        await file.download_to_memory(out=byte_stream)
        audio_bytes = byte_stream.getvalue()
        
        prompt = "Transcribe verbatim. Use ONLY Cyrillic script (Russian). NEVER use Latin transliteration."
        
        if skill == "english":
            if bot_mode == "to_english":
                prompt = "Transcribe verbatim. Use ONLY Cyrillic script (Russian). NEVER use Latin transliteration."
            else: 
                prompt = "Transcribe verbatim in English using standard Latin script. Do not translate, just transcribe exactly what is said."
                
        elif skill == "kazakh":
            if bot_mode == "to_kazakh":
                prompt = "Transcribe verbatim. Use ONLY Cyrillic script (Russian). NEVER use Latin transliteration."
            else: 
                prompt = "Transcribe verbatim. Use ONLY Cyrillic script with special Kazakh letters (Ә,Ғ,Қ,Ң,Ө,Ұ,Ү,Һ,І). NEVER use Latin transliteration."
        
        def _transcribe():
            return client.models.generate_content(
                model=MODEL_NAME, 
                contents=[types.Content(role="user", parts=[
                    types.Part.from_text(text=prompt), types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg")
                ])]
            )
            
        response = await asyncio.to_thread(_transcribe)
        return response.text
    except Exception as e:
        logger.error(f"Transcription Error: {e}")
        return "[Аудио не распознано]"

class BotHandler:
    async def get_keyboard(self, uid: int) -> ReplyKeyboardMarkup:
        state = await memory_manager.state_db.get_state(uid)
        skill, bot_mode = state.get("active_skill", "logos"), state.get("bot_mode", "teacher")
        
        if state.get("is_dialogue") or bot_mode == "dialogue": 
            return ReplyKeyboardMarkup([[KeyboardButton("🔴 Выйти из диалога")]], resize_keyboard=True)
            
        rows = []
        if skill in ["english", "kazakh"]:
            lang_btns = [KeyboardButton("🇷🇺➡️🇬🇧 RU->EN"), KeyboardButton("🇬🇧➡️🇷🇺 EN->RU")] if skill == "english" else [KeyboardButton("🇷🇺➡️🇰🇿 RU->KZ"), KeyboardButton("🇰🇿➡️🇷🇺 KZ->RU")]
            rows.extend([lang_btns, [KeyboardButton("🎙 Режим диалога"), KeyboardButton("◀️ Назад в меню")]])
            return ReplyKeyboardMarkup(rows, resize_keyboard=True)

        skills_values = list(SKILL_MAPPING.values())
        for i in range(0, len(skills_values), 3):
            rows.append([KeyboardButton(name) for name in skills_values[i:i+3]])
            
        rows.append([KeyboardButton("📊 Дашборд"), KeyboardButton("🧘 Дыхание"), KeyboardButton("❄️ Вим Хоф")])
        rows.append([KeyboardButton("❓ Помощь")])
        return ReplyKeyboardMarkup(rows, resize_keyboard=True)

    async def show_dashboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE = None, edit=False):
        uid = update.effective_user.id
        state = await memory_manager.state_db.get_state(uid)
        
        is_pro = whitelist.is_admin(uid) or (state.get('subscription_end_date') and state['subscription_end_date'].replace(tzinfo=timezone.utc) > datetime.now(timezone.utc))
        status = "🌟 PRO (Безлимит)" if is_pro else f"🆓 Базовый ({state.get('msg_count', 0)}/{FREE_MESSAGE_LIMIT})"
        
        tokens = state.get('tokens_used', 0)
        chars = state.get('tts_chars', 0)
        
        text = (f"📊 **DASHBOARD**\nID: `{uid}`\nСтатус: **{status}**\nТекущий навык: **{SKILL_MAPPING.get(state.get('active_skill'), 'logos')}**\n"
                f"Инкогнито: **{'ВКЛ 🕵️' if state.get('is_incognito') else 'ВЫКЛ'}**\nРучная память: **{'✅' if state.get('manual_memory') else '❌ Пусто'}**\n"
                f"Расход: 🧠 `{tokens}` токенов | 🗣 `{chars}` симв. аудио")
        
        kb = [
            [InlineKeyboardButton(f"🕵️ Инкогнито: {'ВКЛ' if state.get('is_incognito') else 'ВЫКЛ'}", callback_data="toggle_incognito")],
            [InlineKeyboardButton("📝 Редактировать профиль", callback_data="edit_manual_memory")],
            [InlineKeyboardButton("♻️ Сбросить контекст", callback_data="lobotomy_confirm")],
            [InlineKeyboardButton("🗑 Стереть все мои данные", callback_data="wipe_request")]
        ]
        
        if not is_pro: 
            kb.insert(0, [InlineKeyboardButton(f"⭐️ Купить PRO ({PRO_PRICE_STARS} Stars)", callback_data="buy_pro")])
        
        reply_markup = InlineKeyboardMarkup(kb)
        if edit: await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
        else: await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query; uid = q.from_user.id
        await q.answer()
        
        if q.data == "buy_pro":
            await context.bot.send_invoice(
                chat_id=uid, title="Optimus PRO", description="Безлимитное общение и генерация аудио на 30 дней.", 
                payload="pro_1_month", provider_token="", currency="XTR", prices=[LabeledPrice("PRO на 30 дней", PRO_PRICE_STARS)]
            )
        elif q.data == "toggle_incognito":
            state = await memory_manager.state_db.get_state(uid)
            await memory_manager.state_db.update_state(uid, {"is_incognito": not state.get("is_incognito", False)})
            await self.show_dashboard(update, context=context, edit=True)
        elif q.data == "lobotomy_confirm":
            state = await memory_manager.state_db.get_state(uid)
            skill = state.get("active_skill", "logos")
            if memory_manager.state_db.pool:
                async with memory_manager.state_db.pool.acquire() as conn:
                    await conn.execute("DELETE FROM chat_history WHERE uid = $1 AND skill = $2", uid, skill)
            await q.edit_message_text("🧹 Контекст текущего навыка очищен.")
        elif q.data == "edit_manual_memory":
            await memory_manager.state_db.update_state(uid, {"bot_mode": "awaiting_memory"})
            await q.message.reply_text("Отправьте текстом факты, которые я должен запомнить о вас (профессия, имя и т.д.). Или /cancel для отмены.")
        elif q.data == "wipe_request":
            kb = [[InlineKeyboardButton("⚠️ ДА, СТЕРЕТЬ ВСЁ", callback_data="wipe_confirm")], [InlineKeyboardButton("❌ Отмена", callback_data="cancel_wipe")]]
            await q.edit_message_text("🚨 **ВНИМАНИЕ!** Это действие необратимо. Ваша история, контекст, память профиля и векторы в базе будут физически удалены.\n\nВы уверены?", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
        elif q.data == "cancel_wipe":
            await self.show_dashboard(update, context=context, edit=True)
        elif q.data == "wipe_confirm":
            await memory_manager.wipe_all_user_data(uid)
            await q.edit_message_text("✅ Все ваши данные были безвозвратно удалены с наших серверов. Чтобы начать заново, отправьте /start.")

    async def route_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        
        if uid in user_rate_limit: return 
        user_rate_limit[uid] = True
        
        state = await memory_manager.state_db.get_state(uid)
        raw_text = update.message.text or ""
        
        is_pro = whitelist.is_admin(uid) or (state.get('subscription_end_date') and state['subscription_end_date'].replace(tzinfo=timezone.utc) > datetime.now(timezone.utc))
        if not is_pro and state.get('msg_count', 0) >= FREE_MESSAGE_LIMIT:
            if raw_text not in ["📊 Дашборд", "❓ Помощь"]:
                kb = [[InlineKeyboardButton(f"⭐️ Купить PRO ({PRO_PRICE_STARS} Stars)", callback_data="buy_pro")]]
                return await update.message.reply_text("🛑 <b>Пробный лимит исчерпан!</b>\nОформите подписку PRO для безлимитного общения.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

        if state.get("bot_mode") == "awaiting_memory" and raw_text:
            if raw_text == "/cancel": 
                await memory_manager.state_db.update_state(uid, {"bot_mode": "teacher"})
            else: 
                await memory_manager.state_db.update_state(uid, {"manual_memory": raw_text, "bot_mode": "teacher"})
            return await update.message.reply_text("✅ Память профиля обновлена!", reply_markup=await self.get_keyboard(uid))

        if raw_text == "◀️ Назад в меню":
            await memory_manager.state_db.update_state(uid, {"active_skill": "logos", "is_dialogue": 0, "bot_mode": "teacher"})
            return await update.message.reply_text("📋 Главное меню:", reply_markup=await self.get_keyboard(uid))
        if raw_text == "📊 Дашборд": return await self.show_dashboard(update, context=context)
        if raw_text == "❓ Помощь": return await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)
        if raw_text == "🧘 Дыхание": return await update.message.reply_text("🧘 Практика дыхания:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Открыть ZenBreath", web_app=WebAppInfo(url="https://zen-breath-pi.vercel.app"))]]))
        if raw_text == "❄️ Вим Хоф": return await update.message.reply_text("❄️ Метод Вима Хофа:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Открыть WimHof", web_app=WebAppInfo(url="https://wim-hof-flax.vercel.app/"))]]))

        clicked_skill = UI_TO_SKILL.get(raw_text)
        if clicked_skill:
            await memory_manager.state_db.update_state(uid, {"active_skill": clicked_skill, "is_dialogue": 0, "bot_mode": "teacher"})
            return await update.message.reply_text(f"✅ Навык активирован: **{SKILL_MAPPING[clicked_skill]}**", reply_markup=await self.get_keyboard(uid), parse_mode=ParseMode.MARKDOWN)

        if raw_text == "🔴 Выйти из диалога":
            await memory_manager.state_db.update_state(uid, {"is_dialogue": 0, "bot_mode": "teacher"})
            return await update.message.reply_text("🔇 Режим диалога завершен.", reply_markup=await self.get_keyboard(uid))
        if raw_text in ["🇷🇺➡️🇬🇧 RU->EN", "🇬🇧➡️🇷🇺 EN->RU", "🇷🇺➡️🇰🇿 RU->KZ", "🇰🇿➡️🇷🇺 KZ->RU"]:
            mode_map = {"🇷🇺➡️🇬🇧 RU->EN": ("english", "to_english"), "🇬🇧➡️🇷🇺 EN->RU": ("english", "to_russian"), 
                        "🇷🇺➡️🇰🇿 RU->KZ": ("kazakh", "to_kazakh"), "🇰🇿➡️🇷🇺 KZ->RU": ("kazakh", "to_russian")}
            n_skill, n_mode = mode_map[raw_text]
            await memory_manager.state_db.update_state(uid, {"active_skill": n_skill, "is_dialogue": 0, "bot_mode": n_mode})
            return await update.message.reply_text(f"🤐 **Переводчик активирован!**\nВсе сообщения будут переводиться и озвучиваться.")
        if raw_text == "🎙 Режим диалога":
            await memory_manager.state_db.update_state(uid, {"is_dialogue": 1, "bot_mode": "dialogue"})
            return await update.message.reply_text("🎙 **Живой диалог активирован!** Отправьте голосовое.", reply_markup=await self.get_keyboard(uid))

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        text, photo_bytes = update.message.text or "", None

        if not is_pro and len(text) > 4000:
            await update.message.reply_text("⚠️ Текст обрезан до 4000 символов.")
            text = text[:4000]

        if update.message.voice:
            current_skill = state.get("active_skill", "logos")
            current_mode = state.get("bot_mode", "teacher")
            text = await transcribe_audio_secure(update.message.voice.file_id, context, uid, current_skill, current_mode)
            if text.startswith("⛔️"): return await update.message.reply_text(text)
            await update.message.reply_text(f"🎤 Вы сказали: {text}")
        elif update.message.photo:
            photo = update.message.photo[-1]
            if not is_pro and photo.file_size and photo.file_size > 10 * 1024 * 1024:
                return await update.message.reply_text("⛔️ Фото слишком большое (максимум 10 МБ).")
            f = await photo.get_file(); b = io.BytesIO(); await f.download_to_memory(out=b)
            photo_bytes, text = b.getvalue(), update.message.caption or "Analyze image."
        elif update.message.document:
            doc = update.message.document
            if not is_pro and doc.file_size and doc.file_size > 20 * 1024 * 1024:
                return await update.message.reply_text("⛔️ Файл слишком большой (максимум 20 МБ).")
            mime = doc.mime_type or ""
            f = await context.bot.get_file(doc.file_id); b = io.BytesIO(); await f.download_to_memory(out=b)
            file_bytes = b.getvalue()
            await update.message.reply_text(f"📄 Анализирую файл: {doc.file_name}...")
            
            try:
                if "pdf" in mime:
                    def _parse_pdf():
                        return client.models.generate_content(
                            model=MODEL_NAME, 
                            contents=[types.Content(role="user", parts=[types.Part.from_bytes(data=file_bytes, mime_type="application/pdf"), types.Part.from_text(text=update.message.caption or "О чем этот документ?")])]
                        )
                    resp = await asyncio.to_thread(_parse_pdf)
                    text = resp.text
                elif "officedocument.wordprocessingml" in mime:
                    import docx2txt
                    text_extracted = await asyncio.to_thread(docx2txt.process, io.BytesIO(file_bytes))
                    text = f"Документ:\n{text_extracted[:8000]}\n\n{update.message.caption or 'Проанализируй.'}"
                elif "spreadsheet" in mime or "excel" in mime:
                    import openpyxl
                    def _parse_excel():
                        wb = openpyxl.load_workbook(io.BytesIO(file_bytes))
                        ws = wb.active
                        return [" | ".join([str(c) for c in row if c is not None]) for row in ws.iter_rows(max_row=50, values_only=True)]
                    rows = await asyncio.to_thread(_parse_excel)
                    text = f"Таблица:\n{chr(10).join(rows)}\n\n{update.message.caption or 'Проанализируй.'}"
                elif "text" in mime:
                    text_decoded = file_bytes.decode("utf-8", errors="ignore")[:8000]
                    text = f"Файл:\n{text_decoded}\n\n{update.message.caption or 'Проанализируй.'}"
                else: return await update.message.reply_text("❌ Формат не поддерживается.")
            except Exception as e: return await update.message.reply_text("❌ Ошибка при чтении файла.")

        if not text and not photo_bytes: return

        skill, bot_mode = state.get("active_skill", "logos"), state.get("bot_mode", "teacher")
        should_voice, voice_target, hist_skill, mem_ctx, dynamic_temp = False, skill, skill, "", 0.5
        use_google_search = False

        if bot_mode in ("to_russian", "to_english", "to_kazakh"):
            target = "russian" if bot_mode == "to_russian" else ("english" if bot_mode == "to_english" else "kazakh")
            sys_prompt = prompt_manager.get_translator_prompt(target)
            voice_target = target
            should_voice, hist_skill, dynamic_temp = True, f"{skill}_translator", 0.1
        elif state.get("is_dialogue") or bot_mode == "dialogue":
            sys_prompt = prompt_manager.get_dialogue_prompt(skill)
            voice_target = skill
            should_voice, hist_skill, dynamic_temp = True, f"{skill}_dialogue", 0.8
        else:
            sys_prompt, hist_skill = prompt_manager.get_teacher_prompt(skill), skill
            mem_ctx = await memory_manager.build_context_prompt(uid, skill, text, state) 
            use_google_search = True
            
            if skill not in NO_VOICE_TEACHER_SKILLS:
                should_voice = True

        full_prompt = f"{sys_prompt}\n\n=== MEMORY ===\n{mem_ctx}" if mem_ctx else sys_prompt
        hist = []
        if bot_mode not in ["to_russian", "to_english", "to_kazakh"]:
            hist = await memory_manager.get_short_term_history(uid, hist_skill, 10)
        
        reply_tokens = 0
        try:
            cnt = [types.Content(role="model" if m["role"]=="assistant" else "user", parts=[types.Part.from_text(text=m["content"])]) for m in hist]
            u_parts = [types.Part.from_text(text=text)]
            if photo_bytes: u_parts.append(types.Part.from_bytes(data=photo_bytes, mime_type="image/jpeg"))
            cnt.append(types.Content(role="user", parts=u_parts))
            
            async with llm_semaphore:
                search_tool = [types.Tool(google_search=types.GoogleSearch())] if use_google_search else None
                gen_config = types.GenerateContentConfig(temperature=dynamic_temp, system_instruction=full_prompt, tools=search_tool)
                
                def _gen(): return client.models.generate_content(model=MODEL_NAME, contents=cnt, config=gen_config)
                resp = await asyncio.wait_for(asyncio.to_thread(_gen), timeout=45.0)
            
            reply = resp.text.strip()
            if hasattr(resp, "usage_metadata") and resp.usage_metadata:
                reply_tokens = getattr(resp.usage_metadata, "total_token_count", 0)
                
        except asyncio.TimeoutError: reply = "⏳ Извините, нейросеть думает слишком долго. Попробуйте перефразировать запрос."
        except Exception as e: 
            logger.error(f"GenAI Error: {e}")
            reply = "⚠️ Произошла ошибка генерации ответа."

        await safe_send(update, reply)
        
        is_incognito = state.get("is_incognito", False)
        await memory_manager.save_interaction(uid, hist_skill, text, reply, is_incognito)

        tts_chars_used = 0
        if should_voice and (len(reply) < 1000 or whitelist.is_admin(uid)):
            target_voice = None
            if bot_mode in ("to_russian", "to_english", "to_kazakh") or state.get("is_dialogue") or bot_mode == "dialogue":
                if voice_target == "english":
                    target_voice = VOICE_MAP["english"]
                elif voice_target == "kazakh":
                    target_voice = VOICE_MAP["kazakh"]
                else:
                    target_voice = VOICE_MAP["russian"]
            else:
                target_voice = SKILL_VOICES.get(skill, VOICE_MAP["russian_male"])
                
            ab = await generate_voice_bytes(reply, target_voice)
            if ab: 
                stream = io.BytesIO(ab); stream.name = "response.mp3"
                try: 
                    await update.message.reply_audio(audio=stream)
                    tts_chars_used = len(clean_text_for_tts(reply))
                except Exception as e: logger.error(f"Telegram Audio Send Error: {e}")

        await memory_manager.state_db.update_economics(uid, reply_tokens, tts_chars_used)

handler = BotHandler()

async def admin_add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not whitelist.is_admin(update.effective_user.id): return
    try:
        tid = int(context.args[0])
        whitelist.add_user(tid)
        await update.message.reply_text(f"✅ Пользователь `{tid}` получил вечный PRO.")
    except Exception: await update.message.reply_text("Формат: /add_user 1234567")

async def admin_del_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not whitelist.is_admin(update.effective_user.id): return
    try:
        tid = int(context.args[0])
        whitelist.remove_user(tid)
        await update.message.reply_text(f"🗑 PRO доступ для `{tid}` аннулирован.")
    except Exception: await update.message.reply_text("Формат: /del_user 1234567")

async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    uid = update.effective_user.id
    
    if await memory_manager.state_db.save_payment(uid, payment.telegram_payment_charge_id, payment.total_amount):
        new_date = datetime.now(timezone.utc) + timedelta(days=30)
        await memory_manager.state_db.update_state(uid, {"subscription_end_date": new_date})
        await memory_manager.state_db.reset_msg_count(uid) 
        
        for admin_id in ROOT_ADMINS:
            try: await context.bot.send_message(chat_id=admin_id, text=f"💰 ДЗЫНЬ! Пользователь {uid} купил PRO за {payment.total_amount} Stars!")
            except Exception: pass
            
        await update.message.reply_text("🎉 **Оплата прошла успешно!** Вы получили PRO на 30 дней.", parse_mode="Markdown")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await memory_manager.state_db.update_state(uid, {"active_skill": "logos", "bot_mode": "teacher"})
    await update.message.reply_text("🚀 Optimus AI Online. Выберите навык:", reply_markup=await handler.get_keyboard(uid))

async def post_init(app: Application):
    await memory_manager.initialize()
    await app.bot.set_my_commands([BotCommand("start", "Меню"), BotCommand("status", "Дашборд")])
    logger.info("🚀 Optimus SaaS Engine Initialized")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    jq = app.job_queue
    jq.run_daily(cron_weekly_english, time=dt_time(hour=19, minute=0, tzinfo=TZ_ASTANA), days=(6,)) 
    jq.run_daily(cron_weekly_kazakh, time=dt_time(hour=19, minute=0, tzinfo=TZ_ASTANA), days=(6,))  
    jq.run_daily(cron_daily_ping, time=dt_time(hour=12, minute=0, tzinfo=TZ_ASTANA)) 
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", handler.show_dashboard))
    app.add_handler(CommandHandler("add_user", admin_add_user))
    app.add_handler(CommandHandler("del_user", admin_del_user))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.add_handler(MessageHandler(filters.TEXT | filters.VOICE | filters.PHOTO | filters.Document.ALL, handler.route_message))
    app.add_handler(CallbackQueryHandler(handler.handle_callback))
    
    app.run_polling(drop_pending_updates=True)