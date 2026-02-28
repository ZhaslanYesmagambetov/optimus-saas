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

# === CONFIGURATION ===
BASE_DIR = Path(__file__).parent
load_dotenv(dotenv_path=BASE_DIR / '.env')

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
AZURE_SPEECH_KEY = os.getenv("AZURE_SPEECH_KEY")
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION")
FREE_MESSAGE_LIMIT = 50
PRO_PRICE_STARS = 250
TZ_ASTANA = timezone(timedelta(hours=5)) # Часовой пояс Астаны

try: ROOT_ADMINS = [int(u) for u in os.getenv("ALLOWED_USERS", "").split(",") if u.strip()]
except ValueError: ROOT_ADMINS = []

WHITELIST_FILE = BASE_DIR / "whitelist.json"
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger("OptimusSaaS")

client = genai.Client(api_key=GOOGLE_API_KEY)
memory_manager = MemoryManager(BASE_DIR)
MODEL_NAME = "gemini-2.0-flash" 
llm_semaphore = asyncio.Semaphore(20)
user_rate_limit = TTLCache(maxsize=10000, ttl=1.5) 

SKILL_MAPPING = {
    "english": "🇬🇧 Английский", "kazakh": "🇰🇿 Казахский", "python": "🐍 Python", "sysadmin": "💻 Сисадмин",
    "network": "🌐 Сети (Cisco)", "fitness": "💪 Фитнес", "nutrition": "🥗 Нутрициолог", "health_consultant": "🏥 Мед. Консультант",
    "psychologist": "🧠 Психолог", "career": "💼 Карьера", "finance": "💵 Финансы", "payroll": "💸 Кадры/Зарплата",
    "accountant": "💰 Бухгалтер", "lawyer": "⚖️ Юрист", "marketing": "📈 Маркетинг", "fishing": "🎣 Рыбалка",
    "math": "🧮 Математика", "study_helper": "🎓 Учеба", "travel": "✈️ Путешествия", "family": "👨‍👩‍👧 Семья",
    "logos": "🏛 Логос", "socrates": "🤔 Сократ", "albert": "🔬 Альберт", "neuron": "🤖 Нейрон",
    "bobby": "💼 Бобби", "don_juan": "🌵 Дон Хуан"
}
UI_TO_SKILL = {v: k for k, v in SKILL_MAPPING.items()}
VOICE_MAP = {"kazakh": "kk-KZ-DauletNeural", "english": "en-US-ChristopherNeural", "russian": "ru-RU-DmitryNeural"}

class PromptManager:
    def __init__(self, base_dir: Path):
        self.prompts_dir = base_dir / "prompts"
        self.cache: Dict[str, str] = {}
    def get_teacher_prompt(self, skill: str) -> str:
        filepath = self.prompts_dir / f"{skill}.txt"
        if filepath.exists(): return filepath.read_text(encoding='utf-8').strip()
        return "SYSTEM: Helpful AI Assistant."
    def get_translator_prompt(self, target_lang: str) -> str:
        filepath = self.prompts_dir / "translate.txt"
        if filepath.exists(): return filepath.read_text(encoding='utf-8').strip().replace("{TARGET_LANGUAGE}", target_lang.upper())
        return f"SYSTEM: TRANSLATE TO {target_lang.upper()}."
    def get_dialogue_prompt(self, skill: str) -> str:
        filepath = self.prompts_dir / "dialogue.txt"
        if filepath.exists(): return filepath.read_text(encoding='utf-8').strip().replace("{LANGUAGE}", skill.upper())
        return "SYSTEM: Conversational partner."

prompt_manager = PromptManager(BASE_DIR)

HELP_TEXT = """
🤖 **Optimus — Многофункциональный AI-Ассистент**

**📚 Навыки:**
Выберите нужный навык из меню — Языки, IT, Здоровье, Финансы, Философия и другие.

**🎙 Голосовой режим:**
Отправьте голосовое сообщение — бот распознает речь и ответит текстом + аудио.

**📸 Анализ изображений:**
Отправьте фото с подписью — бот проанализирует его.

**🌐 Переводчик:**
В языковых навыках используйте кнопки RU→EN/KZ или EN/KZ→RU.

**🎙 Диалог:**
Режим живого разговора на иностранном языке.

**📊 Дашборд:** /status — статус подписки, память, инкогнито.

**🕵️ Инкогнито:**
Режим без сохранения истории и фактов.

**❓ Поддержка:** Если что-то не работает — напишите администратору.
"""

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
        self.dynamic_users.add(uid); self.save()
    def remove_user(self, uid: int):
        if uid in self.dynamic_users: self.dynamic_users.remove(uid); self.save()

whitelist = WhitelistManager()

# --- ФОНОВЫЕ ЗАДАЧИ (CRON JOBS) ---
async def cron_weekly_english(context: ContextTypes.DEFAULT_TYPE):
    """Рассылка саммари по английскому каждое воскресенье"""
    logger.info("⏳ Запуск еженедельного саммари (English)...")
    dialogs = await memory_manager.short_term.get_history_for_period("english", days=7)
    
    sys_prompt = "Ты AI-учитель английского. Проанализируй этот диалог за неделю. Напиши ОЧЕНЬ короткое и дружелюбное саммари на русском. Если есть грамматические ошибки - укажи на них. Если ошибок нет - просто похвали. В конце задай 2-3 коротких вопроса по английскому для поддержания диалога."
    
    for uid, history_text in dialogs.items():
        if len(history_text) < 50: continue # Слишком мало общались
        try:
            resp = await asyncio.to_thread(client.models.generate_content, model=MODEL_NAME, 
                contents=history_text, config=types.GenerateContentConfig(temperature=0.7, system_instruction=sys_prompt))
            
            await context.bot.send_message(chat_id=uid, text=f"🌟 **Еженедельный отчет по Английскому!**\n\n{resp.text}", parse_mode=ParseMode.MARKDOWN)
            await asyncio.sleep(2) # Защита от Flood Limit
        except Exception as e: logger.error(f"Cron Error UID {uid}: {e}")

async def cron_weekly_kazakh(context: ContextTypes.DEFAULT_TYPE):
    """Легкое напоминание по казахскому"""
    dialogs = await memory_manager.short_term.get_history_for_period("kazakh", days=7)
    for uid in dialogs.keys():
        try:
            await context.bot.send_message(chat_id=uid, text="🇰🇿 Привет! На этой неделе мы неплохо потренировали казахский язык. Хочешь продолжить с того места, где мы остановились?")
            await asyncio.sleep(1)
        except Exception: pass

async def cron_daily_ping(context: ContextTypes.DEFAULT_TYPE):
    """Пинг неактивных пользователей (3 дня)"""
    inactive_uids = await memory_manager.state_db.get_inactive_users(days=3)
    for uid in inactive_uids:
        try:
            await context.bot.send_message(chat_id=uid, text="👋 Привет! Давненько не общались. Я всегда здесь, если захочешь попрактиковать языки или задать вопрос!")
            await asyncio.sleep(1)
        except Exception: pass

# --- ОСНОВНАЯ ЛОГИКА БОТА ---
def clean_text_for_tts(text: str) -> str:
    text = re.sub(r'^(Аударма|Перевод|Translation)[:!]\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'[\*\_`#]', '', text)
    return re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text).strip()

async def generate_voice_bytes(text: str, voice: str) -> Optional[bytes]:
    clean_text = clean_text_for_tts(text)
    if not clean_text or len(clean_text) < 2 or not AZURE_SPEECH_KEY: return None
    try:
        def _tts():
            cfg = speechsdk.SpeechConfig(subscription=AZURE_SPEECH_KEY, region=AZURE_SPEECH_REGION)
            cfg.speech_synthesis_voice_name = voice
            cfg.set_speech_synthesis_output_format(speechsdk.SpeechSynthesisOutputFormat.Audio16Khz32KBitRateMonoMp3)
            res = speechsdk.SpeechSynthesizer(speech_config=cfg, audio_config=None).speak_text_async(clean_text).get()
            return res.audio_data if res.reason == speechsdk.ResultReason.SynthesizingAudioCompleted else None
        return await asyncio.to_thread(_tts)
    except Exception: return None

async def safe_send(update: Update, text: str, use_markdown: bool = True):
    MAX = 3500
    for i in range(0, len(text), MAX):
        chunk = text[i:i+MAX]
        try: await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN if use_markdown else None)
        except Exception: await update.message.reply_text(chunk) 

class BotHandler:
    async def get_keyboard(self, uid: int) -> ReplyKeyboardMarkup:
        state = await memory_manager.state_db.get_state(uid)
        skill, bot_mode = state.get("active_skill", "logos"), state.get("bot_mode", "teacher")
        if state.get("is_dialogue") or bot_mode == "dialogue": return ReplyKeyboardMarkup([[KeyboardButton("🔴 Выйти из диалога")]], resize_keyboard=True)
        rows = []
        if skill in ["english", "kazakh"]:
            lang = "EN" if skill == "english" else "KZ"
            rows.extend([[KeyboardButton(f"🇷🇺➡️{lang} RU->{lang}"), KeyboardButton(f"{lang}➡️🇷🇺 {lang}->RU")], [KeyboardButton("🎙 Режим диалога"), KeyboardButton("◀️ Назад в меню")]])
        else:
            keys = list(SKILL_MAPPING.keys())
            for i in range(0, len(keys), 2):
                rows.append([KeyboardButton(SKILL_MAPPING[keys[i]]), KeyboardButton(SKILL_MAPPING[keys[i+1]])] if i+1 < len(keys) else [KeyboardButton(SKILL_MAPPING[keys[i]])])
            rows.extend([[KeyboardButton("📊 Дашборд"), KeyboardButton("🧘 Дыхание")], [KeyboardButton("❓ Помощь")]])
        return ReplyKeyboardMarkup(rows, resize_keyboard=True)

    async def show_dashboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE = None, edit=False):
        uid = update.effective_user.id
        state = await memory_manager.state_db.get_state(uid)
        
        is_pro = whitelist.is_admin(uid) or (state.get('subscription_end_date') and state['subscription_end_date'].replace(tzinfo=timezone.utc) > datetime.now(timezone.utc))
        status = "🌟 PRO (Безлимит)" if is_pro else f"🆓 Базовый ({state.get('msg_count', 0)}/{FREE_MESSAGE_LIMIT})"
        
        text = (f"📊 **DASHBOARD**\nID: `{uid}`\nСтатус: **{status}**\nТекущий навык: **{SKILL_MAPPING.get(state.get('active_skill'), 'logos')}**\n"
                f"Инкогнито: **{'ВКЛ 🕵️' if state.get('is_incognito') else 'ВЫКЛ'}**\nРучная память: **{'✅' if state.get('manual_memory') else '❌ Пусто'}**")
        
        kb = [[InlineKeyboardButton(f"🕵️ Инкогнито: {'ВКЛ' if state.get('is_incognito') else 'ВЫКЛ'}", callback_data="toggle_incognito")],
              [InlineKeyboardButton("📝 Редактировать память", callback_data="edit_manual_memory")],
              [InlineKeyboardButton("♻️ Сбросить контекст", callback_data="lobotomy_confirm")]]
        
        if not is_pro: kb.insert(0, [InlineKeyboardButton(f"⭐️ Купить PRO ({PRO_PRICE_STARS} Stars)", callback_data="buy_pro")])
        
        if edit: await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
        else: await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query; uid = q.from_user.id; await q.answer()
        if q.data == "buy_pro":
            await context.bot.send_invoice(chat_id=uid, title="Optimus PRO", description="Безлимитное общение и генерация аудио на 30 дней.", 
                                           payload="pro_1_month", provider_token="", currency="XTR", prices=[LabeledPrice("PRO на 30 дней", PRO_PRICE_STARS)])
        elif q.data == "toggle_incognito":
            state = await memory_manager.state_db.get_state(uid)
            await memory_manager.state_db.update_state(uid, {"is_incognito": not state.get("is_incognito", False)})
            await self.show_dashboard(update, edit=True)
        elif q.data == "lobotomy_confirm":
            state = await memory_manager.state_db.get_state(uid)
            await memory_manager.short_term.clear_history(uid, state.get("active_skill", "logos"))
            await q.edit_message_text("🧹 Контекст очищен.")
        elif q.data == "edit_manual_memory":
            await memory_manager.state_db.update_state(uid, {"bot_mode": "awaiting_memory"})
            await q.message.reply_text("Отправьте текстом всё, что я должен запомнить о вас намертво. Или /cancel для отмены.")

    async def route_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if uid in user_rate_limit: return 
        user_rate_limit[uid] = True
        
        state = await memory_manager.state_db.get_state(uid)
        raw_text = update.message.text or ""
        
        # --- БЛОК ОПЛАТЫ (PAYWALL) ---
        is_pro = whitelist.is_admin(uid) or (state.get('subscription_end_date') and state['subscription_end_date'].replace(tzinfo=timezone.utc) > datetime.now(timezone.utc))
        if not is_pro and state.get('msg_count', 0) >= FREE_MESSAGE_LIMIT:
            if raw_text not in ["📊 Дашборд", "❓ Помощь"]:
                kb = [[InlineKeyboardButton(f"⭐️ Купить PRO ({PRO_PRICE_STARS} Stars)", callback_data="buy_pro")]]
                return await update.message.reply_text("🛑 <b>Пробный лимит исчерпан!</b>\nОформите подписку PRO для безлимитного общения.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

        if state.get("bot_mode") == "awaiting_memory" and raw_text:
            if raw_text == "/cancel": await memory_manager.state_db.update_state(uid, {"bot_mode": "teacher"})
            else: await memory_manager.state_db.update_state(uid, {"manual_memory": raw_text, "bot_mode": "teacher"})
            return await update.message.reply_text("✅ Память сохранена!", reply_markup=await self.get_keyboard(uid))

        if raw_text == "◀️ Назад в меню":
            await memory_manager.state_db.update_state(uid, {"active_skill": "logos", "is_dialogue": 0, "bot_mode": "teacher"})
            return await update.message.reply_text("📋 Главное меню:", reply_markup=await self.get_keyboard(uid))

        if raw_text == "📊 Дашборд": return await self.show_dashboard(update)
        if raw_text == "❓ Помощь": return await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.MARKDOWN)
        if raw_text == "🧘 Дыхание": return await update.message.reply_text("Дыхание:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Открыть ZenBreath", web_app=WebAppInfo(url="https://zen-breath-pi.vercel.app"))]]))

        clicked_skill = UI_TO_SKILL.get(raw_text)
        if clicked_skill:
            await memory_manager.state_db.update_state(uid, {"active_skill": clicked_skill, "is_dialogue": 0, "bot_mode": "teacher"})
            return await update.message.reply_text(f"✅ Навык активирован: **{SKILL_MAPPING[clicked_skill]}**", reply_markup=await self.get_keyboard(uid), parse_mode=ParseMode.MARKDOWN)

        if raw_text == "🔴 Выйти из диалога":
            await memory_manager.state_db.update_state(uid, {"is_dialogue": 0, "bot_mode": "teacher"})
            return await update.message.reply_text("🔇 Режим диалога завершен.", reply_markup=await self.get_keyboard(uid))
        if "RU->" in raw_text:
            await memory_manager.state_db.update_state(uid, {"active_skill": "english" if "EN" in raw_text else "kazakh", "is_dialogue": 0, "bot_mode": "direct"})
            return await update.message.reply_text("🤐 **Переводчик активирован!**")
        if "->RU" in raw_text:
            await memory_manager.state_db.update_state(uid, {"active_skill": "english" if "EN" in raw_text else "kazakh", "is_dialogue": 0, "bot_mode": "reverse"})
            return await update.message.reply_text("🤐 **Переводчик на русский активирован!**")
        if raw_text == "🎙 Режим диалога":
            await memory_manager.state_db.update_state(uid, {"is_dialogue": 1, "bot_mode": "dialogue"})
            return await update.message.reply_text("🎙 **Диалог активирован!**", reply_markup=await self.get_keyboard(uid))

        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        text, photo_bytes = update.message.text or "", None
        if update.message.voice:
            try:
                f = await context.bot.get_file(update.message.voice.file_id); b = io.BytesIO(); await f.download_to_memory(out=b)
                resp = await asyncio.to_thread(client.models.generate_content, model=MODEL_NAME, contents=[types.Content(role="user", parts=[types.Part.from_text(text="Transcribe verbatim."), types.Part.from_bytes(data=b.getvalue(), mime_type="audio/ogg")])])
                text = resp.text
                await update.message.reply_text(f"🎤 Вы сказали: {text}")
            except Exception: return
        elif update.message.photo:
            f = await update.message.photo[-1].get_file(); b = io.BytesIO(); await f.download_to_memory(out=b)
            photo_bytes, text = b.getvalue(), update.message.caption or "Analyze image."

        if not text and not photo_bytes: return

        skill, bot_mode = state.get("active_skill", "logos"), state.get("bot_mode", "teacher")
        should_voice, voice_target, hist_skill, mem_ctx, dynamic_temp = False, skill, skill, "", 0.5

        if bot_mode == "direct": sys_prompt, should_voice, hist_skill, dynamic_temp = prompt_manager.get_translator_prompt(skill), True, f"{skill}_translator", 0.1
        elif bot_mode == "reverse": sys_prompt, should_voice, voice_target, hist_skill, dynamic_temp = prompt_manager.get_translator_prompt("russian"), True, "russian", f"{skill}_translator", 0.1
        elif state.get("is_dialogue") or bot_mode == "dialogue": sys_prompt, should_voice, hist_skill, dynamic_temp = prompt_manager.get_dialogue_prompt(skill), True, f"{skill}_dialogue", 0.8
        else:
            sys_prompt, hist_skill = prompt_manager.get_teacher_prompt(skill), skill
            mem_ctx = await memory_manager.build_context_prompt(uid, skill, text)
            if skill in ["logos", "socrates", "albert", "neuron", "don_juan"]: should_voice = True

        full_prompt = f"{sys_prompt}\n\n=== MEMORY ===\n{mem_ctx}" if mem_ctx else sys_prompt
        hist = await memory_manager.short_term.get_chat_history(uid, hist_skill, 10) if bot_mode not in ["direct", "reverse"] else []
        
        try:
            cnt = [types.Content(role="model" if m["role"]=="assistant" else "user", parts=[types.Part.from_text(text=m["content"])]) for m in hist]
            u_parts = [types.Part.from_text(text=text)]
            if photo_bytes: u_parts.append(types.Part.from_bytes(data=photo_bytes, mime_type="image/jpeg"))
            cnt.append(types.Content(role="user", parts=u_parts))
            
            async with llm_semaphore:
                # Включаем нативный выход в интернет (Google Search Grounding)
                gen_config = types.GenerateContentConfig(
                    temperature=dynamic_temp, 
                    system_instruction=full_prompt,
                    tools=[{"google_search": {}}]
                )
                resp = await asyncio.wait_for(
                    asyncio.to_thread(
                        client.models.generate_content, 
                        model=MODEL_NAME, 
                        contents=cnt, 
                        config=gen_config
                    ), 
                    timeout=30.0
                )
            reply = resp.text.strip()
        except Exception as e: 
            logger.error(f"GenAI Error: {e}")
            reply = "⚠️ Ошибка генерации ответа или поиска."

        await safe_send(update, reply)
        await memory_manager.process_interaction(uid, hist_skill, text, reply)

        if should_voice and len(reply) < 1000:
            v_lang = "russian" if (bot_mode == "reverse" or skill in ["logos", "socrates", "albert", "neuron", "don_juan"]) else voice_target
            ab = await generate_voice_bytes(reply, VOICE_MAP.get(v_lang, VOICE_MAP["russian"]))
            if ab: 
                stream = io.BytesIO(ab); stream.name = "response.mp3"
                try: await update.message.reply_audio(audio=stream)
                except Exception: pass

handler = BotHandler()

# --- ADMIN COMMANDS ---
async def admin_add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not whitelist.is_admin(update.effective_user.id): return
    try:
        tid = int(context.args[0]); whitelist.add_user(tid)
        await update.message.reply_text(f"✅ Пользователь `{tid}` получил вечный PRO доступ.")
    except Exception: await update.message.reply_text("Формат: /add_user 1234567")

async def admin_del_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not whitelist.is_admin(update.effective_user.id): return
    try:
        tid = int(context.args[0]); whitelist.remove_user(tid)
        await update.message.reply_text(f"🗑 PRO доступ для `{tid}` аннулирован.")
    except Exception: await update.message.reply_text("Формат: /del_user 1234567")

# --- PAYMENT WEBHOOKS ---
async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment, uid = update.message.successful_payment, update.effective_user.id
    if await memory_manager.state_db.save_payment(uid, payment.telegram_payment_charge_id, payment.total_amount):
        new_date = datetime.now(timezone.utc) + timedelta(days=30)
        await memory_manager.state_db.update_state(uid, {"subscription_end_date": new_date})
        await memory_manager.state_db.reset_msg_count(uid) 
        
        for admin_id in ROOT_ADMINS:
            try: await context.bot.send_message(chat_id=admin_id, text=f"💰 ДЗЫНЬ! Пользователь {uid} купил PRO за {payment.total_amount} Stars!")
            except Exception: pass
            
        await update.message.reply_text("🎉 **Оплата прошла успешно!** Вам выдан статус PRO на 30 дней.", parse_mode="Markdown")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await memory_manager.state_db.update_state(uid, {"active_skill": "logos", "bot_mode": "teacher"})
    await update.message.reply_text("🚀 Optimus SaaS Online. Выберите навык:", reply_markup=await handler.get_keyboard(uid))

async def post_init(app: Application):
    await memory_manager.initialize()
    await app.bot.set_my_commands([BotCommand("start", "Меню"), BotCommand("status", "Дашборд")])
    logger.info("🚀 Optimus SaaS Online")

if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    # Регистрация Планировщика (Cron)
    jq = app.job_queue
    jq.run_daily(cron_weekly_english, time=dt_time(hour=19, minute=0, tzinfo=TZ_ASTANA), days=(6,)) # Воскресенье 19:00
    jq.run_daily(cron_weekly_kazakh, time=dt_time(hour=19, minute=0, tzinfo=TZ_ASTANA), days=(6,))  # Воскресенье 19:00
    jq.run_daily(cron_daily_ping, time=dt_time(hour=12, minute=0, tzinfo=TZ_ASTANA)) # Каждый день 12:00
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", handler.show_dashboard))
    app.add_handler(CommandHandler("add_user", admin_add_user))
    app.add_handler(CommandHandler("del_user", admin_del_user))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.add_handler(MessageHandler(filters.TEXT | filters.VOICE | filters.PHOTO, handler.route_message))
    app.add_handler(CallbackQueryHandler(handler.handle_callback))
    app.run_polling()
