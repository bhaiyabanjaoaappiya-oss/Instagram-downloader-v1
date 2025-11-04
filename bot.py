""" Instagram Downloader Telegram Bot (all-in-one enhanced) Features included:

Download public Instagram posts/reels (single or batch URLs)

Persistent per-chat settings (mode: media/document, caption on/off)

Persistent stats (downloads count, bytes sent, last activity)

Rate limiting per-chat

Auto-cleaner background thread to remove temp dirs older than TEMP_MAX_AGE_MIN

Thumbnail collage generation for multi-photo posts (Pillow)

Hashtag extractor from captions

/settings UI with inline buttons

/stats command


Dependencies:

instaloader

pytelegrambotapi

pillow


Install: pip install instaloader pytelegrambotapi pillow

Run: export BOT_TOKEN=<telegram-bot-token> # optional for private posts you follow # export IG_USER=your_ig_user # export IG_PASS=your_ig_pass python instagram_downloader_bot_full.py

Note: Use responsibly. Only download/share content you have rights to. Instagram scraping may be restricted by Instagram's Terms. """

import os import re import time import json import html import shutil import tempfile import threading from typing import List, Tuple, Dict, Any from datetime import datetime, timedelta

import instaloader from instaloader import Post, InstaloaderContext from PIL import Image import telebot from telebot.types import (InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo)

-------------------- Configuration --------------------

BOT_TOKEN = os.getenv("BOT_TOKEN") if not BOT_TOKEN: raise SystemExit("Please export BOT_TOKEN=<your_telegram_bot_token>")

DATA_DIR = os.getenv("DATA_DIR", "./data") os.makedirs(DATA_DIR, exist_ok=True)

SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json") STATS_FILE = os.path.join(DATA_DIR, "stats.json") TEMP_DIR = os.getenv("TEMP_DIR", None) or tempfile.gettempdir() TEMP_PREFIX = os.getenv("TEMP_PREFIX", "ig_dl_") TEMP_MAX_AGE_MIN = int(os.getenv("TEMP_MAX_AGE_MIN", "30"))  # cleanup age RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "5")) TELEGRAM_ALBUM_MAX = 10 OWNER_ID = os.getenv("OWNER_ID")  # optional: for admin features

IG_USER = os.getenv("IG_USER") IG_PASS = os.getenv("IG_PASS")

-------------------- Utilities & Stores --------------------

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

URL_RE = re.compile(r"(https?://(?:www.)?instagram.com/(?:p|reel|tv)/[A-Za-z0-9_-]+)", re.IGNORECASE) SHORT_REDIR_RE = re.compile(r"(https?://(?:www.)?(?:instagr.am|instagram.com)/(?:p|reel|tv)/[A-Za-z0-9_-]+)", re.IGNORECASE)

Thread-safe JSON store

class JSONStore: def init(self, path: str, default: dict = None): self.path = path self.lock = threading.RLock() self.data = default or {} self._load()

def _load(self):
    if os.path.isfile(self.path):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}

def _save(self):
    tmp = self.path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(self.data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, self.path)

def get(self, key, default=None):
    with self.lock:
        return self.data.get(str(key), default)

def set(self, key, value):
    with self.lock:
        self.data[str(key)] = value
        self._save()

def update_subkey(self, key, subkey, value):
    with self.lock:
        k = str(key)
        if k not in self.data:
            self.data[k] = {}
        self.data[k][subkey] = value
        self._save()

def inc(self, key, subkey, amount=1):
    with self.lock:
        k = str(key)
        if k not in self.data:
            self.data[k] = {}
        self.data[k][subkey] = self.data[k].get(subkey, 0) + amount
        self._save()

PREFS = JSONStore(SETTINGS_FILE, default={}) STATS = JSONStore(STATS_FILE, default={})

Rate limiting token buckets

TOKENS: Dict[int, Tuple[int, float]] = {}  # chat_id -> (tokens, last_refill_ts) TOKENS_LOCK = threading.Lock()

def rate_ok(chat_id: int) -> bool: now = time.time() with TOKENS_LOCK: tokens, last = TOKENS.get(chat_id, (RATE_LIMIT_PER_MIN, now)) if now - last >= 60: tokens = RATE_LIMIT_PER_MIN last = now if tokens <= 0: TOKENS[chat_id] = (tokens, last) return False tokens -= 1 TOKENS[chat_id] = (tokens, last) return True

-------------------- Instagram download helper --------------------

SHORTCODE_RE = re.compile(r"instagram.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)")

def extract_shortcode(url: str) -> str: m = SHORTCODE_RE.search(url) if not m: raise ValueError("Invalid/unsupported Instagram URL (needs /p/, /reel/, or /tv/).") return m.group(1)

def _guess_mediacount(post: Post) -> int: try: return post.mediacount except Exception: try: return len(list(post.get_sidecar_nodes())) except Exception: return 1

def download_instagram_media(url: str, login_user: str = None, login_pass: str = None) -> Tuple[str, List[str], Dict[str, Any]]: """ Downloads media into a dedicated temp folder and returns (tmpdir, files, meta) Caller must delete tmpdir when done. """ shortcode = extract_shortcode(url)

L = instaloader.Instaloader(
    download_comments=False,
    save_metadata=False,
    compress_json=False,
    post_metadata_txt_pattern="",
    dirname_pattern="{target}",
    filename_pattern="{shortcode}_{mediaid}"
)

if login_user and login_pass:
    try:
        L.login(login_user, login_pass)
    except Exception:
        pass

ctx: InstaloaderContext = L.context
post = Post.from_shortcode(ctx, shortcode)

tmpdir = tempfile.mkdtemp(prefix=f"{TEMP_PREFIX}{shortcode}_")

cwd = os.getcwd()
try:
    os.chdir(tmpdir)
    L.download_post(post, target="dl")
finally:
    os.chdir(cwd)

media_dir = os.path.join(tmpdir, "dl")
files: List[str] = []
if os.path.isdir(media_dir):
    for name in sorted(os.listdir(media_dir)):
        nl = name.lower()
        if nl.endswith((".jpg", ".jpeg", ".png", ".mp4")):
            files.append(os.path.join(media_dir, name))

if not files:
    raise RuntimeError("No downloadable media found for this URL.")

meta: Dict[str, Any] = {
    "shortcode": shortcode,
    "owner_username": getattr(post, "owner_username", None),
    "caption": (post.caption or "").strip() if hasattr(post, "caption") else "",
    "date_utc": getattr(post, "date_utc", None),
    "mediacount": _guess_mediacount(post),
    "permalink": f"https://www.instagram.com/p/{shortcode}/",
    "is_video": getattr(post, "is_video", False),
}

return tmpdir, files, meta

-------------------- Collage & hashtag helpers --------------------

def create_collage_image(image_paths: List[str], out_path: str, size: int = 800) -> str: """Create a square collage (up to 4 images -> 2x2). Returns path to saved image.""" imgs = [Image.open(p).convert("RGB") for p in image_paths[:4]] n = len(imgs) if n == 0: raise ValueError("No images to make collage")

# decide grid
grid = (1, 1)
if n == 1:
    grid = (1, 1)
elif n == 2:
    grid = (2, 1)
else:
    grid = (2, 2)

cols, rows = grid
thumb_w = size // cols
thumb_h = size // rows

new_im = Image.new('RGB', (thumb_w * cols, thumb_h * rows), (255, 255, 255))

i = 0
for r in range(rows):
    for c in range(cols):
        if i >= n:
            break
        img = imgs[i].copy()
        img.thumbnail((thumb_w, thumb_h), Image.Resampling.LANCZOS)
        # center paste
        x = c * thumb_w + (thumb_w - img.width) // 2
        y = r * thumb_h + (thumb_h - img.height) // 2
        new_im.paste(img, (x, y))
        i += 1

new_im.save(out_path, format='JPEG', quality=85)
for im in imgs:
    try:
        im.close()
    except Exception:
        pass
return out_path

HASHTAG_RE = re.compile(r"#([\w\u00C0-\u024F]+)")

def extract_hashtags(text: str, max_tags: int = 10) -> List[str]: if not text: return [] tags = [f"#{t}" for t in HASHTAG_RE.findall(text)] # keep unique preserving order seen = set() out = [] for t in tags: if t.lower() not in seen: seen.add(t.lower()) out.append(t) if len(out) >= max_tags: break return out

-------------------- Bot helpers --------------------

def sanitize_url(text: str) -> str | None: m = URL_RE.search(text) or SHORT_REDIR_RE.search(text) if not m: return None url = m.group(1) if not url.endswith('/'): url += '/' url = url.split('?')[0].split('#')[0] return url

def fmt_meta_caption(meta: dict, include_body: bool) -> str: owner = meta.get('owner_username') or 'unknown' count = meta.get('mediacount', 1) link = html.escape(meta.get('permalink', '')) sc = html.escape(meta.get('shortcode', '')) header = f"<b>Instagram</b> • @{html.escape(owner)}  •  {count} media" body = "" if include_body: cap = (meta.get('caption') or '').strip() if cap: if len(cap) > 900: cap = cap[:900].rstrip() + '…' body = '\n' + html.escape(cap) footer = f"\n<a href="{link}">Link</a>  |  <code>{sc}</code>" return header + body + footer

Settings keyboard

def settings_keyboard(chat_id: int) -> InlineKeyboardMarkup: mode = PREFS.get(chat_id, {}).get('mode', 'media') cap = 'on' if PREFS.get(chat_id, {}).get('caption_on', True) else 'off' kb = InlineKeyboardMarkup() kb.row(InlineKeyboardButton(f"Mode: {mode} (tap to toggle)", callback_data=f"toggle:mode")) kb.row(InlineKeyboardButton(f"Caption: {cap} (tap to toggle)", callback_data=f"toggle:caption")) kb.row(InlineKeyboardButton("Clear my stats", callback_data="clear:stats")) return kb

-------------------- Background cleaner --------------------

def temp_cleaner_worker(): while True: try: cutoff = time.time() - (TEMP_MAX_AGE_MIN * 60) for name in os.listdir(TEMP_DIR): if not name.startswith(TEMP_PREFIX): continue path = os.path.join(TEMP_DIR, name) try: mtime = os.path.getmtime(path) if mtime < cutoff: if os.path.isdir(path): shutil.rmtree(path, ignore_errors=True) else: try: os.remove(path) except Exception: pass except Exception: pass except Exception: pass time.sleep(60)  # run every minute

cleaner_thread = threading.Thread(target=temp_cleaner_worker, daemon=True) cleaner_thread.start()

-------------------- Commands --------------------

---------- Channel-join verification (configurable) ----------

Set either CHANNEL_USERNAME (preferred) or CHANNEL_ID. Also you can control enforcement mode:

CHANNEL_VERIFICATION_MODE = 'force' (default) -> block until joined

CHANNEL_VERIFICATION_MODE = 'soft' -> show warning but allow continue

CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")  # e.g. "@MyChannel" CHANNEL_ID = os.getenv("CHANNEL_ID")  # e.g. -1001234567890 CHANNEL_VERIFICATION_MODE = os.getenv("CHANNEL_VERIFICATION_MODE", "force").lower()

def user_is_member_of_channel(user_id: int) -> bool: """Return True if CHANNEL_USERNAME/CHANNEL_ID not configured or user is member/subscriber. If channel not configured then verification is considered disabled. """ if not CHANNEL_USERNAME and not CHANNEL_ID: return True  # verification disabled try: target = CHANNEL_ID or CHANNEL_USERNAME member = bot.get_chat_member(target, user_id) if member and member.status in ('creator', 'administrator', 'member', 'restricted'): return True return False except Exception: # If we cannot check due to API error or bot lack of access, treat based on mode: # - 'force': treat as not verified (block) # - 'soft': allow through (warn only) return False if CHANNEL_VERIFICATION_MODE == 'force' else True

def ensure_channel_join_prompt(chat_id: int, user_id: int): """If user not member, send a prompt with join button and a "I've Joined ✅" re-check button.""" # If verification mode is soft, send a one-time warning but allow usage if CHANNEL_VERIFICATION_MODE == 'soft': bot.send_message(chat_id, ( "⚠️ It looks like you haven't joined our channel. You can still use the bot, but please consider joining to support us. " "Join: " + (CHANNEL_USERNAME or str(CHANNEL_ID) or "(channel)") )) return True

# For 'force' mode, require join
if user_is_member_of_channel(user_id):
    return True
join_target = CHANNEL_USERNAME or CHANNEL_ID or ""
text = (
    "Please join our channel to continue using this bot.

" "After joining, tap <b>I've Joined ✅</b> to continue." ) kb = InlineKeyboardMarkup() if CHANNEL_USERNAME: kb.row(InlineKeyboardButton("Join Channel 🔗", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}") ) elif CHANNEL_ID: kb.row(InlineKeyboardButton("Open Channel", url="https://t.me/")) kb.row(InlineKeyboardButton("I've Joined ✅ — Check", callback_data="check:joined")) bot.send_message(chat_id, text, reply_markup=kb) return False

@bot.message_handler(commands=['start', 'help']) def cmd_start(msg): cid = msg.chat.id uid = msg.from_user.id # initialize defaults if PREFS.get(cid) is None: PREFS.set(cid, {'mode': 'media', 'caption_on': True})

# Channel verification: if configured, require membership
if not user_is_member_of_channel(uid):
    ensure_channel_join_prompt(cid, uid)
    return

bot.reply_to(msg, (
    "👋 Send me a public Instagram post/reel/tv URL (single or multiple lines) and I’ll fetch the media.

" "Commands: " "• /settings — quick toggles (mode, caption) " "• /mode media|document — set default send mode " "• /stats — show your usage stats

" "<i>Only download/share content you’re allowed to. Scraping may be restricted by Instagram's Terms.</i>" ), reply_markup=settings_keyboard(cid))

@bot.message_handler(commands=['settings']) def cmd_settings(msg): bot.reply_to(msg, "⚙️ Settings", reply_markup=settings_keyboard(msg.chat.id))

@bot.callback_query_handler(func=lambda c: c.data and (c.data.startswith('toggle:') or c.data.startswith('clear:') or c.data.startswith('check:'))) def on_toggle(cb): chat_id = cb.message.chat.id if cb.data == 'toggle:mode': cur = PREFS.get(chat_id, {}).get('mode', 'media') new_mode = 'document' if cur == 'media' else 'media' PREFS.update_subkey(chat_id, 'mode', new_mode) bot.answer_callback_query(cb.id, f"Mode → {new_mode}") elif cb.data == 'toggle:caption': cur = PREFS.get(chat_id, {}).get('caption_on', True) PREFS.update_subkey(chat_id, 'caption_on', not cur) bot.answer_callback_query(cb.id, f"Caption → {'on' if not cur else 'off'}") elif cb.data == 'clear:stats': STATS.set(chat_id, {}) bot.answer_callback_query(cb.id, "Stats cleared") elif cb.data == 'check:joined': # Re-check membership for the user who pressed the button user_id = cb.from_user.id if user_is_member_of_channel(user_id): bot.answer_callback_query(cb.id, "Thanks — membership confirmed ✅") try: bot.edit_message_text("Thank you for joining! Use /start to begin.", chat_id, cb.message.message_id) except Exception: pass else: bot.answer_callback_query(cb.id, "Still not a member — please join the channel first.") # refresh keyboard try: bot.edit_message_reply_markup(chat_id, cb.message.message_id, reply_markup=settings_keyboard(chat_id)) except Exception: pass

@bot.message_handler(commands=['mode']) def cmd_mode(msg): parts = msg.text.strip().split(maxsplit=1) if len(parts) == 1: mode = PREFS.get(msg.chat.id, {}).get('mode', 'media') bot.reply_to(msg, f"Current mode: <b>{mode}</b>\nUse /mode media or /mode document.") return choice = parts[1].strip().lower() if choice not in ('media', 'document'): bot.reply_to(msg, "Use: /mode media  or  /mode document") return PREFS.update_subkey(msg.chat.id, 'mode', choice) bot.reply_to(msg, f"✅ Mode updated to <b>{choice}</b>")

@bot.message_handler(commands=['stats']) def cmd_stats(msg): cid = msg.chat.id s = STATS.get(cid, {}) or {} downloads = s.get('downloads', 0) bytes_sent = s.get('bytes_sent', 0) last = s.get('last_activity') last_str = 'never' if not last else last mode = PREFS.get(cid, {}).get('mode', 'media') caption_on = PREFS.get(cid, {}).get('caption_on', True) reply = ( f"📊 Stats for this chat:\n" f"• Downloads: {downloads}\n" f"• Data sent: {round(bytes_sent / (1024*1024), 2)} MB\n" f"• Last activity: {last_str}\n" f"• Mode: {mode}\n" f"• Caption: {'on' if caption_on else 'off'}" ) bot.reply_to(msg, reply)

-------------------- Core handler (single or multiple URLs) --------------------

@bot.message_handler(func=lambda m: bool(URL_RE.search(m.text or '') or SHORT_REDIR_RE.search(m.text or ''))) def handle_instagram(msg): chat_id = msg.chat.id user_id = msg.from_user.id

# Channel verification: if configured, require membership before proceeding
if not user_is_member_of_channel(user_id):
    ensure_channel_join_prompt(chat_id, user_id)
    return

if not rate_ok(chat_id):
    bot.reply_to(msg, "⏳ Slow down a bit — too many requests. Try again in a minute.")
    return

text = msg.text or ''
urls = []
# allow multiple URLs (newline-separated or space-separated)
for line in re.split(r'[

,;]+', text): u = sanitize_url(line) if u: urls.append(u) if not urls: bot.reply_to(msg, "Please send a valid Instagram URL.") return

notice = bot.reply_to(msg, f"Fetching {len(urls)} link(s)…")

total_bytes = 0
downloads = 0
try:
    for idx, url in enumerate(urls, start=1):
        # progress edit
        try:
            bot.edit_message_text(f"Fetching {idx}/{len(urls)}: <code>{html.escape(url)}</code>", chat_id, notice.message_id)
        except Exception:
            pass

        tmpdir, files, meta = download_instagram_media(url, IG_USER, IG_PASS)
        mode = PREFS.get(chat_id, {}).get('mode', 'media')
        caption_on = PREFS.get(chat_id, {}).get('caption_on', True)
        caption = fmt_meta_caption(meta, include_body=caption_on)

        # Extract hashtags
        tags = extract_hashtags(meta.get('caption', ''))
        tags_line = '

' + ' '.join(tags) if tags else ''

# If multiple photos, create collage
        photo_files = [f for f in files if f.lower().endswith(('.jpg', '.jpeg', '.png'))]
        collage_path = None
        if len(photo_files) > 1:
            try:
                collage_path = os.path.join(tmpdir, 'collage.jpg')
                create_collage_image(photo_files, collage_path)
            except Exception:
                collage_path = None

        # Send
        if mode == 'document':
            # send caption first
            bot.send_message(chat_id, caption + tags_line, disable_web_page_preview=True)
            for path in files[:TELEGRAM_ALBUM_MAX]:
                with open(path, 'rb') as fh:
                    bot.send_document(chat_id, fh)
                    fh.seek(0, os.SEEK_END)
                    total_bytes += fh.tell()
                    downloads += 1
        else:
            # media mode
            if collage_path:
                # send collage first with caption
                with open(collage_path, 'rb') as fh:
                    bot.send_photo(chat_id, fh, caption=caption + tags_line)
                    fh.seek(0, os.SEEK_END)
                    total_bytes += fh.tell()
                    downloads += 1
                # then send rest as media group (up to TELEGRAM_ALBUM_MAX)
                media_items = []
                count = 0
                for path in files[:TELEGRAM_ALBUM_MAX]:
                    if path == collage_path:
                        continue
                    if path.lower().endswith('.mp4'):
                        with open(path, 'rb') as fh:
                            bot.send_video(chat_id, fh)
                            fh.seek(0, os.SEEK_END)
                            total_bytes += fh.tell()
                            downloads += 1
                    else:
                        with open(path, 'rb') as fh:
                            bot.send_photo(chat_id, fh)
                            fh.seek(0, os.SEEK_END)
                            total_bytes += fh.tell()
                            downloads += 1
                    count += 1
                    if count >= TELEGRAM_ALBUM_MAX:
                        break
            else:
                # no collage
                if len(files) == 1:
                    path = files[0]
                    with open(path, 'rb') as fh:
                        if path.lower().endswith('.mp4'):
                            bot.send_video(chat_id, fh, caption=caption + tags_line)
                        else:
                            bot.send_photo(chat_id, fh, caption=caption + tags_line)
                        fh.seek(0, os.SEEK_END)
                        total_bytes += fh.tell()
                        downloads += 1
                else:
                    # multiple -> try to send media_group
                    media_group = []
                    sent_count = 0
                    for path in files[:TELEGRAM_ALBUM_MAX]:
                        if path.lower().endswith('.mp4'):
                            with open(path, 'rb') as fh:
                                bot.send_video(chat_id, fh)
                                fh.seek(0, os.SEEK_END)
                                total_bytes += fh.tell()
                                downloads += 1
                        else:
                            with open(path, 'rb') as fh:
                                bot.send_photo(chat_id, fh)
                                fh.seek(0, os.SEEK_END)
                                total_bytes += fh.tell()
                                downloads += 1
                        sent_count += 1
                        if sent_count >= TELEGRAM_ALBUM_MAX:
                            break

        # update stats per url
        STATS.inc(chat_id, 'downloads', downloads)
        STATS.inc(chat_id, 'bytes_sent', total_bytes)
        STATS.update_subkey(chat_id, 'last_activity', datetime.utcnow().isoformat() + 'Z')

        # cleanup this tmpdir
        if os.path.isdir(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)

    bot.edit_message_text("Done ✅", chat_id, notice.message_id)
except Exception as e:
    try:
        bot.edit_message_text(f"⚠️ Error: {html.escape(str(e))}", chat_id, notice.message_id)
    except Exception:
        bot.reply_to(msg, f"⚠️ Error: {html.escape(str(e))}")
finally:
    # ensure any leftover tmpdirs from partial runs are cleaned below by cleaner
    pass

-------------------- Start polling --------------------

if name == 'main': print('Bot starting...') bot.infinity_polling(skip_pending=True)
