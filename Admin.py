import os
import re
import sqlite3
import logging
import asyncio
from datetime import datetime, timedelta
import urllib.parse
import urllib.request
import json
import pytz

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

# ==================== CONFIGURATION ====================
BOT_TOKEN = "8716929402:AAHjMmDKsvl9fD14JelWZBEtOuS60qmumM4"
GPLINKS_API_KEY = "61b6868075976f926d6ad36958b4d6b1b0c413ee"
BOT_USERNAME = "ScamProofXBot"
ADMIN_CHAT_ID = 1393373043
GROUP_ID = -1004304967558  # Your specified Telegram Group Chat ID

DB_PATH = "cineflix.db"
TIMEZONE = pytz.timezone("Asia/Kolkata")

# Setup Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Memory state management for multi-step admin flows
ADMIN_STATES = {}
ANNOUNCEMENT_DRAFTS = {}

# ==================== DATABASE INITIALIZATION ====================
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    # Users Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            first_seen TEXT,
            last_seen TEXT,
            search_count INTEGER DEFAULT 0,
            search_history TEXT DEFAULT ''
        )
    """)
    
    # Files Table (Movies & APKs)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_key TEXT UNIQUE,
            title TEXT,
            file_id TEXT,
            file_type TEXT,
            category TEXT,
            created_at TEXT
        )
    """)
    
    # Bad Words Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bad_words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT UNIQUE
        )
    """)
    
    # Banned Users Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS banned_users (
            user_id INTEGER PRIMARY KEY
        )
    """)
    
    # Requests Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            requested_item TEXT,
            requested_at TEXT
        )
    """)
    
    # Announcements Table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS announcements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_chat_id INTEGER,
            message_id INTEGER,
            start_time TEXT,
            end_time TEXT,
            status TEXT DEFAULT 'pending',
            posted_message_id INTEGER DEFAULT NULL
        )
    """)
    
    conn.commit()
    conn.close()

# ==================== HELPER FUNCTIONS ====================
def normalize_string(text: str) -> str:
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r'[^a-z0-9]', '', text)
    return text.strip()

def create_gplink(destination_url: str) -> str:
    encoded_url = urllib.parse.quote(destination_url)
    api_url = f"https://gplinks.in/api?api={GPLINKS_API_KEY}&url={encoded_url}"
    try:
        req = urllib.request.Request(api_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            if data.get("status") == "success" and data.get("shortlink"):
                return data.get("shortlink")
    except Exception as e:
        logger.error(f"GPLinks API Exception: {e}")
    return destination_url

def update_user_activity(user):
    conn = get_db()
    cursor = conn.cursor()
    now_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user.id,))
    row = cursor.fetchone()
    
    if row:
        cursor.execute("""
            UPDATE users SET first_name = ?, username = ?, last_seen = ? WHERE user_id = ?
        """, (user.first_name, user.username or "N/A", now_str, user.id))
    else:
        cursor.execute("""
            INSERT INTO users (user_id, first_name, username, first_seen, last_seen, search_count, search_history)
            VALUES (?, ?, ?, ?, ?, 0, '')
        """, (user.id, user.first_name, user.username or "N/A", now_str, now_str))
        
    conn.commit()
    conn.close()

def is_user_banned(user_id: int) -> bool:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM banned_users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    return res is not None

def check_and_delete_bad_words(text: str) -> bool:
    if not text:
        return False
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT word FROM bad_words")
    rows = cursor.fetchall()
    conn.close()
    
    normalized_text = normalize_string(text)
    
    for row in rows:
        bad_word = row["word"]
        norm_bad = normalize_string(bad_word)
        if norm_bad and norm_bad in normalized_text:
            return True
        pattern = r'\b' + re.escape(bad_word) + r'\b'
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False

# ==================== ADMIN KEYBOARDS ====================
def get_main_admin_keyboard():
    keyboard = [
        [InlineKeyboardButton("📊 Dashboard", callback_data="adm_dashboard"), InlineKeyboardButton("📢 Group Announcement", callback_data="adm_announcement")],
        [InlineKeyboardButton("📲 Upload APK", callback_data="adm_upload_apk"), InlineKeyboardButton("🚫 Add Bad Word", callback_data="adm_add_badword")],
        [InlineKeyboardButton("📁 Manage Files", callback_data="adm_manage_files"), InlineKeyboardButton("🚫 Manage Bad Words", callback_data="adm_manage_badwords")],
        [InlineKeyboardButton("📥 Pending Requests", callback_data="adm_requests"), InlineKeyboardButton("📢 Scheduled Announcements", callback_data="adm_list_announcements")],
        [InlineKeyboardButton("👥 Manage Users", callback_data="adm_users"), InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast")],
        [InlineKeyboardButton("❌ Close Panel", callback_data="adm_close")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel (/1)", callback_data="adm_cancel")]])

# ==================== COMMAND HANDLERS ====================
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_CHAT_ID:
        return
    ADMIN_STATES[user_id] = None
    ANNOUNCEMENT_DRAFTS[user_id] = None
    await update.message.reply_text("❌ Operation cancelled.", reply_markup=get_main_admin_keyboard())

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    update_user_activity(user)
    
    if context.args:
        arg = context.args[0]
        if arg.startswith("file_"):
            file_key = arg.replace("file_", "")
            
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM files WHERE file_key = ?", (file_key,))
            file_data = cursor.fetchone()
            conn.close()
            
            if file_data:
                file_id = file_data["file_id"]
                file_type = file_data["file_type"]
                title = file_data["title"]
                
                try:
                    await update.message.reply_text(f"✅ **{title}**\n\nHere is your file:")
                    if file_type == "video":
                        await context.bot.send_video(chat_id=user.id, video=file_id)
                    else:
                        await context.bot.send_document(chat_id=user.id, document=file_id)
                except Exception as e:
                    logger.error(f"Error sending file via start: {e}")
                    await update.message.reply_text("❌ Error delivering file. Please make sure you have started the bot in private chat.")
            else:
                await update.message.reply_text("❌ File not found or link expired.")
            return

    await update.message.reply_text("👋 **Welcome to CineFlix Bot!**\nSearch for any movie or APK in our official group.")

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ You are not authorized.")
        return
    ADMIN_STATES[user_id] = None
    await update.message.reply_text("⚡ **CineFlix Admin Panel**", reply_markup=get_main_admin_keyboard())

# ==================== GROUP MESSAGE & SEARCH HANDLER ====================
async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
        
    chat = update.effective_chat
    user = update.effective_user
    text = update.message.text or update.message.caption or ""
    
    # Handle Bad Words Check
    if check_and_delete_bad_words(text):
        try:
            # Check if user is admin in group
            member = await context.bot.get_chat_member(chat.id, user.id)
            if member.status not in ["creator", "administrator"]:
                await update.message.delete()
                return
        except Exception as e:
            logger.error(f"Bad word deletion error: {e}")

    # Auto Save Videos/Documents forwarded/uploaded to group by admins
    if update.message.video or update.message.document:
        try:
            member = await context.bot.get_chat_member(chat.id, user.id)
            if member.status in ["creator", "administrator"]:
                doc = update.message.video or update.message.document
                title = update.message.caption or getattr(doc, 'file_name', None) or "Untitled File"
                file_id = doc.file_id
                file_type = "video" if update.message.video else "document"
                
                key = normalize_string(title)
                if key:
                    now_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
                    conn = get_db()
                    cursor = conn.cursor()
                    cursor.execute("""
                        INSERT INTO files (file_key, title, file_id, file_type, category, created_at)
                        VALUES (?, ?, ?, ?, 'movie', ?)
                        ON CONFLICT(file_key) DO UPDATE SET title=excluded.title, file_id=excluded.file_id
                    """, (key, title, file_id, file_type, now_str))
                    conn.commit()
                    conn.close()
        except Exception as e:
            logger.error(f"Auto save error: {e}")

    if not text or text.startswith("/"):
        return

    # Check if user is banned
    if is_user_banned(user.id):
        return

    update_user_activity(user)
    search_key = normalize_string(text)
    if not search_key:
        return

    # Database Search Logic (Exact Match -> Partial Match)
    conn = get_db()
    cursor = conn.cursor()
    
    # 1. Exact Match
    cursor.execute("SELECT * FROM files WHERE file_key = ?", (search_key,))
    file_data = cursor.fetchone()
    
    # 2. Partial Match
    if not file_data:
        cursor.execute("SELECT * FROM files WHERE file_key LIKE ?", (f"%{search_key}%",))
        file_data = cursor.fetchone()
        
    if not file_data:
        cursor.execute("SELECT * FROM files WHERE title LIKE ?", (f"%{text}%",))
        file_data = cursor.fetchone()

    # Track search in user history
    cursor.execute("SELECT search_history, search_count FROM users WHERE user_id = ?", (user.id,))
    urow = cursor.fetchone()
    if urow:
        new_count = urow["search_count"] + 1
        history = urow["search_history"]
        history_list = [h for h in history.split(";") if h]
        history_list.append(text)
        new_history = ";".join(history_list[-10:])  # keep last 10
        cursor.execute("UPDATE users SET search_count = ?, search_history = ? WHERE user_id = ?", (new_count, new_history, user.id))
        conn.commit()

    if file_data:
        conn.close()
        deep_link = f"https://t.me/{BOT_USERNAME}?start=file_{file_data['file_key']}"
        short_link = create_gplink(deep_link)
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎬 Download / Watch File", url=short_link)]
        ])
        
        await update.message.reply_text(
            f"🔍 **{file_data['title'].upper()}** পাওয়া গেছে!\n\n"
            f"👇 নিচের button-এ click করে 3-step verification complete করুন।",
            reply_markup=keyboard
        )
    else:
        # Check duplicate request cooldown
        now_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("SELECT requested_at FROM requests WHERE user_id = ? AND requested_item = ? ORDER BY id DESC LIMIT 1", (user.id, text))
        last_req = cursor.fetchone()
        
        should_notify = True
        if last_req:
            try:
                last_time = datetime.strptime(last_req["requested_at"], "%Y-%m-%d %H:%M:%S")
                if datetime.now(TIMEZONE).replace(tzinfo=None) - last_time < timedelta(hours=1):
                    should_notify = False
            except Exception:
                pass

        cursor.execute("""
            INSERT INTO requests (user_id, username, requested_item, requested_at)
            VALUES (?, ?, ?, ?)
        """, (user.id, user.username or "N/A", text, now_str))
        conn.commit()
        conn.close()

        await update.message.reply_text("Movie is not available please wait 1 day")

        if should_notify:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=f"⚠️ **New File Request Alert!**\n\n"
                         f"👤 **User:** {user.first_name} (@{user.username or 'N/A'})\n"
                         f"🆔 **Chat ID:** `{user.id}`\n"
                         f"🔍 **Requested Item:** `{text}`"
                )
            except Exception as e:
                logger.error(f"Admin request alert failed: {e}")

# ==================== ADMIN CALLBACK HANDLER ====================
async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    if user_id != ADMIN_CHAT_ID:
        await query.answer("Unauthorized", show_alert=True)
        return

    data = query.data

    if data == "adm_cancel":
        ADMIN_STATES[user_id] = None
        ANNOUNCEMENT_DRAFTS[user_id] = None
        await query.edit_message_text("⚡ **CineFlix Admin Panel**", reply_markup=get_main_admin_keyboard())

    elif data == "adm_close":
        await query.edit_message_text("❌ Admin Panel Closed.")

    elif data == "adm_dashboard":
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM users"); total_users = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM files WHERE category='movie'"); total_movies = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM files WHERE category='apk'"); total_apks = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM bad_words"); total_badwords = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM requests"); total_requests = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM announcements WHERE status='pending'"); total_ann = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM banned_users"); total_banned = cursor.fetchone()[0]
        conn.close()

        dash_text = (
            f"📊 **CineFlix Bot Dashboard**\n\n"
            f"👥 **Total Users:** {total_users}\n"
            f"🎬 **Total Movies:** {total_movies}\n"
            f"📲 **Total APKs:** {total_apks}\n"
            f"🚫 **Total Bad Words:** {total_badwords}\n"
            f"📥 **Pending Requests:** {total_requests}\n"
            f"📢 **Scheduled Announcements:** {total_ann}\n"
            f"🚷 **Banned Users:** {total_banned}"
        )
        await query.edit_message_text(dash_text, reply_markup=get_main_admin_keyboard())

    elif data == "adm_upload_apk":
        ADMIN_STATES[user_id] = "apk_waiting_name"
        await query.edit_message_text("📲 **Upload APK**\n\nAPK-এর নাম লিখে পাঠান।", reply_markup=get_cancel_keyboard())

    elif data == "adm_add_badword":
        ADMIN_STATES[user_id] = "badword_waiting_input"
        await query.edit_message_text("🚫 **Add Bad Word**\n\nBad word পাঠান। একাধিক হলে comma (,) দিয়ে পাঠাতে পারেন।", reply_markup=get_cancel_keyboard())

    elif data == "adm_announcement":
        ADMIN_STATES[user_id] = "ann_waiting_content"
        await query.edit_message_text("📢 **Group Announcement**\n\nAnnouncement পাঠান (Text, photo, video অথবা document)।", reply_markup=get_cancel_keyboard())

    elif data == "adm_broadcast":
        ADMIN_STATES[user_id] = "broadcast_waiting_content"
        await query.edit_message_text("📢 **Broadcast Message**\n\nসব ইউজারের কাছে যে মেসেজ পাঠাতে চান তা পাঠান:", reply_markup=get_cancel_keyboard())

    elif data == "adm_manage_badwords":
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM bad_words")
        words = cursor.fetchall()
        conn.close()

        if not words:
            await query.edit_message_text("🚫 কোনো Bad Word ডাটাবেসে নেই।", reply_markup=get_main_admin_keyboard())
            return

        keyboard = []
        for w in words:
            keyboard.append([
                InlineKeyboardButton(w["word"], callback_data="noop"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"del_bw_{w['id']}")
            ])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="adm_cancel")])
        await query.edit_message_text("🚫 **Manage Bad Words:**", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("del_bw_"):
        bw_id = int(data.replace("del_bw_", ""))
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bad_words WHERE id = ?", (bw_id,))
        conn.commit()
        conn.close()
        await query.answer("Deleted!")
        await admin_callback_handler(update, context)

    elif data == "adm_manage_files":
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM files ORDER BY id DESC LIMIT 20")
        files = cursor.fetchall()
        conn.close()

        if not files:
            await query.edit_message_text("📁 কোনো ফাইল পাওয়া যায়নি।", reply_markup=get_main_admin_keyboard())
            return

        keyboard = []
        for f in files:
            label = f"[{f['category'].upper()}] {f['title'][:20]}"
            keyboard.append([
                InlineKeyboardButton(label, callback_data="noop"),
                InlineKeyboardButton("🗑 Delete", callback_data=f"del_file_{f['id']}")
            ])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="adm_cancel")])
        await query.edit_message_text("📁 **Manage Files (Last 20):**", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("del_file_"):
        file_id = int(data.replace("del_file_", ""))
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM files WHERE id = ?", (file_id,))
        conn.commit()
        conn.close()
        await query.answer("File deleted!")
        await admin_callback_handler(update, context)

    elif data == "adm_requests":
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM requests ORDER BY id DESC LIMIT 10")
        reqs = cursor.fetchall()
        conn.close()

        if not reqs:
            await query.edit_message_text("📥 কোনো Pending Request নেই।", reply_markup=get_main_admin_keyboard())
            return

        msg = "📥 **Pending Requests:**\n\n"
        keyboard = []
        for r in reqs:
            msg += f"• `{r['requested_item']}` by @{r['username']} ({r['requested_at']})\n"
            keyboard.append([InlineKeyboardButton(f"🗑 Delete '{r['requested_item'][:15]}'", callback_data=f"del_req_{r['id']}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="adm_cancel")])
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data.startswith("del_req_"):
        req_id = int(data.replace("del_req_", ""))
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM requests WHERE id = ?", (req_id,))
        conn.commit()
        conn.close()
        await query.answer("Request cleared!")
        await admin_callback_handler(update, context)

    elif data == "adm_list_announcements":
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM announcements WHERE status='pending'")
        anns = cursor.fetchall()
        conn.close()

        if not anns:
            await query.edit_message_text("📢 কোনো Active Announcement Schedule নেই।", reply_markup=get_main_admin_keyboard())
            return

        keyboard = []
        msg = "📢 **Scheduled Announcements:**\n\n"
        for a in anns:
            msg += f"ID: {a['id']} | Start: {a['start_time']} | End: {a['end_time']}\n"
            keyboard.append([InlineKeyboardButton(f"🗑 Cancel #{a['id']}", callback_data=f"del_ann_{a['id']}")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="adm_cancel")])
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("del_ann_"):
        ann_id = int(data.replace("del_ann_", ""))
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE announcements SET status='cancelled' WHERE id = ?", (ann_id,))
        conn.commit()
        conn.close()
        await query.answer("Announcement Cancelled!")
        await admin_callback_handler(update, context)

    elif data == "adm_users":
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users ORDER BY last_seen DESC LIMIT 10")
        users = cursor.fetchall()
        conn.close()

        msg = "👥 **Manage Recent Users:**\n\n"
        keyboard = []
        for u in users:
            banned = is_user_banned(u['user_id'])
            ban_btn = InlineKeyboardButton("✅ Unban" if banned else "🚫 Ban", callback_data=f"toggle_ban_{u['user_id']}")
            msg += f"• {u['first_name']} (@{u['username']}) - ID: `{u['user_id']}` | Searches: {u['search_count']}\n"
            keyboard.append([
                InlineKeyboardButton(f"{u['first_name'][:15]}", callback_data="noop"),
                ban_btn
            ])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="adm_cancel")])
        await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data.startswith("toggle_ban_"):
        target_uid = int(data.replace("toggle_ban_", ""))
        conn = get_db()
        cursor = conn.cursor()
        if is_user_banned(target_uid):
            cursor.execute("DELETE FROM banned_users WHERE user_id = ?", (target_uid,))
            await query.answer("User Unbanned!")
        else:
            cursor.execute("INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)", (target_uid,))
            await query.answer("User Banned!")
        conn.commit()
        conn.close()
        await admin_callback_handler(update, context)

    elif data == "confirm_broadcast":
        b_msg = context.user_data.get("broadcast_msg")
        if not b_msg:
            await query.edit_message_text("❌ Broadcast expired.", reply_markup=get_main_admin_keyboard())
            return
            
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users")
        all_users = cursor.fetchall()
        conn.close()

        sent = 0
        failed = 0
        await query.edit_message_text("🚀 Broadcasting in progress...")

        for u in all_users:
            try:
                await context.bot.copy_message(
                    chat_id=u["user_id"],
                    from_chat_id=b_msg.chat_id,
                    message_id=b_msg.message_id
                )
                sent += 1
                await asyncio.sleep(0.05) # Rate limit protection
            except Exception:
                failed += 1

        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"✅ **Broadcast Completed!**\n\n🟢 Sent: {sent}\n🔴 Failed/Blocked: {failed}",
            reply_markup=get_main_admin_keyboard()
        )

# ==================== ADMIN INPUT HANDLER ====================
async def handle_admin_inputs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_CHAT_ID:
        return

    state = ADMIN_STATES.get(user_id)
    if not state:
        return

    text = update.message.text or ""

    # 1. APK Flow
    if state == "apk_waiting_name":
        context.user_data["temp_apk_name"] = text
        ADMIN_STATES[user_id] = "apk_waiting_file"
        await update.message.reply_text("✅ APK name successfully saved.\n\nএখন APK file upload করুন।", reply_markup=get_cancel_keyboard())

    elif state == "apk_waiting_file":
        if update.message.document:
            apk_name = context.user_data.get("temp_apk_name", "App")
            file_id = update.message.document.file_id
            file_key = normalize_string(apk_name)
            now_str = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO files (file_key, title, file_id, file_type, category, created_at)
                VALUES (?, ?, ?, 'document', 'apk', ?)
                ON CONFLICT(file_key) DO UPDATE SET title=excluded.title, file_id=excluded.file_id
            """, (file_key, apk_name, file_id, now_str))
            conn.commit()
            conn.close()

            ADMIN_STATES[user_id] = None
            await update.message.reply_text(f"✅ APK **{apk_name}** successfully added to database!", reply_markup=get_main_admin_keyboard())

    # 2. Bad Words Flow
    elif state == "badword_waiting_input":
        words = [w.strip() for w in text.split(",") if w.strip()]
        conn = get_db()
        cursor = conn.cursor()
        added = 0
        for w in words:
            try:
                cursor.execute("INSERT INTO bad_words (word) VALUES (?)", (w.lower(),))
                added += 1
            except Exception:
                pass
        conn.commit()
        conn.close()

        ADMIN_STATES[user_id] = None
        await update.message.reply_text(f"✅ {added} Bad word(s) successfully added!", reply_markup=get_main_admin_keyboard())

    # 3. Announcement Flow
    elif state == "ann_waiting_content":
        ANNOUNCEMENT_DRAFTS[user_id] = {"msg_id": update.message.message_id, "chat_id": update.message.chat_id}
        ADMIN_STATES[user_id] = "ann_waiting_time"
        await update.message.reply_text(
            "⏰ **Start and End time দিন।**\n\n"
            "Example:\n`Start 18:15 End 19:00`\nঅথবা:\n`18:15 19:00`",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard()
        )

    elif state == "ann_waiting_time":
        match = re.search(r'(\d{1,2}:\d{2}).*?(\d{1,2}:\d{2})', text)
        if match:
            start_str, end_str = match.group(1), match.group(2)
            draft = ANNOUNCEMENT_DRAFTS.get(user_id)
            
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO announcements (from_chat_id, message_id, start_time, end_time, status)
                VALUES (?, ?, ?, ?, 'pending')
            """, (draft["chat_id"], draft["msg_id"], start_str, end_str))
            conn.commit()
            conn.close()

            ADMIN_STATES[user_id] = None
            ANNOUNCEMENT_DRAFTS[user_id] = None
            await update.message.reply_text(
                f"✅ **Announcement scheduled successfully!**\n\n🟢 Start: {start_str}\n🔴 End: {end_str}",
                reply_markup=get_main_admin_keyboard()
            )
        else:
            await update.message.reply_text("❌ Time format incorrect. Try again (e.g., `Start 18:15 End 19:00`).")

    # 4. Broadcast Flow
    elif state == "broadcast_waiting_content":
        context.user_data["broadcast_msg"] = update.message
        ADMIN_STATES[user_id] = None
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Confirm", callback_data="confirm_broadcast"), InlineKeyboardButton("❌ Cancel", callback_data="adm_cancel")]
        ])
        await update.message.reply_text("❓ Send this broadcast message to ALL users?", reply_markup=keyboard)

# ==================== ANNOUNCEMENT SCHEDULER ENGINE ====================
async def announcement_scheduler_task(app):
    while True:
        try:
            now_dt = datetime.now(TIMEZONE)
            current_hhmm = now_dt.strftime("%H:%M")

            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM announcements WHERE status = 'pending'")
            pending = cursor.fetchall()

            for ann in pending:
                ann_id = ann["id"]
                start_time = ann["start_time"]
                end_time = ann["end_time"]
                posted_msg_id = ann["posted_message_id"]

                # Post Announcement
                if not posted_msg_id and current_hhmm == start_time:
                    try:
                        sent_msg = await app.bot.copy_message(
                            chat_id=GROUP_ID,
                            from_chat_id=ann["from_chat_id"],
                            message_id=ann["message_id"]
                        )
                        cursor.execute("UPDATE announcements SET posted_message_id = ? WHERE id = ?", (sent_msg.message_id, ann_id))
                        conn.commit()
                        logger.info(f"Posted announcement #{ann_id}")
                    except Exception as e:
                        logger.error(f"Error posting announcement #{ann_id}: {e}")

                # Delete Announcement
                if posted_msg_id and current_hhmm == end_time:
                    try:
                        await app.bot.delete_message(chat_id=GROUP_ID, message_id=posted_msg_id)
                    except Exception as e:
                        logger.error(f"Error deleting announcement #{ann_id}: {e}")
                    cursor.execute("UPDATE announcements SET status = 'completed' WHERE id = ?", (ann_id,))
                    conn.commit()
                    logger.info(f"Completed announcement #{ann_id}")

            conn.close()
        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")

        await asyncio.sleep(20)

# ==================== MAIN EXECUTION ====================
async def post_init(app):
    init_db()
    asyncio.create_task(announcement_scheduler_task(app))

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    # Base Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("dwip", admin_command))
    app.add_handler(CommandHandler("1", cancel_command))

    # Callbacks
    app.add_handler(CallbackQueryHandler(admin_callback_handler))

    # Admin Input Filter
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (~filters.COMMAND), handle_admin_inputs))

    # Group Search & Filtering
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & (~filters.COMMAND), handle_group_message))

    print("🚀 CineFlix Bot is active and running...")
    app.run_polling()

if __name__ == "__main__":
    main()
