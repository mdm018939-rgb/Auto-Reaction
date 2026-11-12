import os, json, random, hashlib, asyncio, logging
from aiohttp import web
from telegram import (Update, Bot, ReplyKeyboardMarkup, KeyboardButton,
                      InlineKeyboardMarkup, InlineKeyboardButton, ReactionTypeEmoji)
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (ApplicationBuilder, Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, TypeHandler, ContextTypes, filters,
                          ApplicationHandlerStop)

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("motherbot")

# ================= কনফিগ =================
MAIN_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ.get("BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")  # https://xxx.onrender.com
PORT = int(os.environ.get("PORT", 8080))
ADMIN_ID = 6625019627
SUPPORT_URL = "https://t.me/TEEM_X_FAST_SUPPORT_BOT?start=start"
BASE_LIMIT, BOOST_LIMIT, REFER_NEED = 3, 50, 2
REACTIONS = [
    "❤", "👍", "👎", "🔥", "🥰", "👏", "😁", "🤔",
    "🤯", "😱", "🤬", "😢", "🎉", "🤩", "🤮", "💩",
    "🙏", "👌", "🕊", "🤡", "🥱", "🥴", "😍", "🐳",
    "❤‍🔥", "🌚", "🌭", "💯", "🤣", "⚡", "🍌", "🏆",
    "💔", "🤨", "😐", "🍓", "🍾", "💋", "😈",
    "😴", "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈",
    "😇", "😨", "🤝", "✍", "🤗", "🫡", "🎅", "🎄",
    "☃", "💅", "🤪", "🗿", "🆒", "💘", "🙉", "🦄",
    "😘", "💊", "🙊", "😎", "👾", "🤷‍♂", "🤷", "🤷‍♀",
    "😡",
]
CH_MOD = -1003426582447
CH_TOKENS = -1003761514798
CH_BOTS = -1003829680946
CH_REFER = -1003905741450


# ================= চ্যানেল স্টোরেজ =================
class ChannelStore:
    LIMIT = 3500  # টেলিগ্রামের সীমা ৪০৯৬, নিরাপদ থাকতে ৩৫০০

    def __init__(self, name, chat_id):
        self.name, self.chat_id = name, chat_id
        self.snap = {}       # key -> JSON স্ট্রিং
        self.msg_of = {}     # key -> মেসেজ আইডি
        self.members = {}    # মেসেজ আইডি -> [key]
        self.index = []      # ডাটা মেসেজের আইডি লিস্ট (১, ২, ৩ ক্রমে)
        self.index_id = None
        self.todo = set()
        self.dirty = set()
        self.index_dirty = False

    # ---------- ইনডেক্স মেসেজ ----------
    def _link(self, mid):
        return f"https://t.me/c/{str(self.chat_id)[4:]}/{mid}"

    def _index_text(self):
        links = "  ".join(f'<a href="{self._link(m)}">{i}</a>'
                          for i, m in enumerate(self.index, 1))
        return f"📂 {self.name.upper()} DATA INDEX\n\n{links}\n\nIDS:{len(self.snap)}"

    # ---------- পড়া ----------
    async def _read(self, bot, mid):
        for _ in range(3):
            try:
                m = await bot.forward_message(self.chat_id, self.chat_id, mid)
                try:
                    await bot.delete_message(self.chat_id, m.message_id)
                except Exception:
                    pass
                return m.text
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Exception as e:
                log.error("%s read %s: %s", self.name, mid, e)
                return None
        return None

    async def load(self, bot):
        chat = await bot.get_chat(self.chat_id)
        pin = chat.pinned_message
        if pin is None:
            m = await bot.send_message(self.chat_id, self._index_text(), parse_mode="HTML")
            await bot.pin_chat_message(self.chat_id, m.message_id, disable_notification=True)
            self.index_id = m.message_id
            return
        if "DATA INDEX" not in (pin.text or ""):
            raise RuntimeError(f"{self.name}: পিন করা মেসেজটা ইনডেক্স নয়")
        self.index_id = pin.message_id
        self.index = []
        for e in (pin.entities or []):
            if e.type == "text_link" and e.url:
                try:
                    self.index.append(int(e.url.rstrip("/").split("/")[-1]))
                except ValueError:
                    pass
        for mid in self.index:
            self.members[mid] = []
            text = await self._read(bot, mid)
            if not text:
                continue
            for line in text.split("\n")[1:]:   # প্রথম লাইন "DATA #n" বাদ
                line = line.strip()
                if not line:
                    continue
                try:
                    k, v = json.loads(line)
                except Exception:
                    continue
                self.snap[k] = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
                self.msg_of[k] = mid
                self.members[mid].append(k)
            await asyncio.sleep(0.1)

    def items(self):
        return {k: json.loads(s) for k, s in self.snap.items()}

    def sync(self, new):
        for k, v in new.items():
            s = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
            if self.snap.get(k) != s:
                if k not in self.snap:
                    self.index_dirty = True   # IDS সংখ্যা বদলাবে
                self.snap[k] = s
                self.todo.add(k)
        for k in [k for k in self.snap if k not in new]:
            del self.snap[k]
            self.todo.add(k)
            self.index_dirty = True

    # ---------- লেখা ----------
    def _render(self, mid):
        n = self.index.index(mid) + 1 if mid in self.index else len(self.index) + 1
        lines = [f"[{json.dumps(k, ensure_ascii=False)},{self.snap[k]}]"
                 for k in self.members.get(mid, []) if k in self.snap]
        return f"DATA #{n}\n" + "\n".join(lines)

    def _attach(self, k, mid):
        self.members[mid].append(k)
        self.msg_of[k] = mid
        self.dirty.add(mid)

    async def _place(self, bot, k):
        need = len(k) + len(self.snap[k]) + 6
        if self.index:
            last = self.index[-1]
            if len(self._render(last)) + need <= self.LIMIT:
                self._attach(k, last)
                return
        # আগের মেসেজ ভরে গেছে, নতুন DATA #n মেসেজ
        m = await bot.send_message(self.chat_id, f"DATA #{len(self.index) + 1}")
        self.index.append(m.message_id)
        self.members[m.message_id] = []
        self.index_dirty = True
        self._attach(k, m.message_id)

    async def _write(self, bot, mid):
        text = self._render(mid)
        while len(text) > self.LIMIT and len(self.members[mid]) > 1:
            k = self.members[mid].pop()
            self.msg_of.pop(k, None)
            self.todo.add(k)
            text = self._render(mid)
        try:
            await bot.edit_message_text(text, chat_id=self.chat_id, message_id=mid)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            return False
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                log.error("%s edit %s: %s", self.name, mid, e)
            return True
        except Exception as e:
            log.error("%s edit %s: %s", self.name, mid, e)
            return False
        return True

    async def flush(self, bot):
        for _ in range(5):
            if not (self.todo or self.dirty or self.index_dirty):
                return
            todo, self.todo = self.todo, set()
            for k in sorted(todo):
                try:
                    if k not in self.snap:
                        mid = self.msg_of.pop(k, None)
                        if mid is not None and k in self.members.get(mid, []):
                            self.members[mid].remove(k)
                            self.dirty.add(mid)
                    elif k in self.msg_of:
                        self.dirty.add(self.msg_of[k])
                    else:
                        await self._place(bot, k)
                except RetryAfter as e:
                    self.todo.add(k)
                    await asyncio.sleep(e.retry_after + 1)
                except Exception as e:
                    self.todo.add(k)
                    log.error("%s place: %s", self.name, e)
            for mid in list(self.dirty):
                self.dirty.discard(mid)
                if not await self._write(bot, mid):
                    self.dirty.add(mid)
                await asyncio.sleep(0.5)
            if self.index_dirty:
                self.index_dirty = False
                try:
                    await bot.edit_message_text(self._index_text(), chat_id=self.chat_id,
                                                message_id=self.index_id, parse_mode="HTML")
                except BadRequest as e:
                    if "not modified" not in str(e).lower():
                        log.error("%s index: %s", self.name, e)
                except Exception as e:
                    self.index_dirty = True
                    log.error("%s index: %s", self.name, e)
class Storage:
    def __init__(self):
        self.data = {"users": {}, "bots": {}}
        self.removed = {}
        self.stores = {}
        self.bot = None
        self.lock = asyncio.Lock()

    async def load(self, bot):
        self.bot = bot
        self.stores = {"mod": ChannelStore("Moderation", CH_MOD),
                       "tokens": ChannelStore("Tokens", CH_TOKENS),
                       "bots": ChannelStore("Bots", CH_BOTS),
                       "refer": ChannelStore("Refer", CH_REFER)}
        for s in self.stores.values():
            await s.load(bot)
        users = self.data["users"]

        def new_user():
            return {"bots": [], "refs": [], "referred_by": None, "banned": False}

        for uid, v in self.stores["mod"].items().items():
            u = new_user()
            u["banned"] = bool(v.get("banned"))
            users[uid] = u
        for uid, v in self.stores["refer"].items().items():
            by = v.get("by")
            if uid in users:
                users[uid]["referred_by"] = by
            r = users.get(str(by))
            if r is not None and int(uid) not in r["refs"]:
                r["refs"].append(int(uid))
        bots_meta = self.stores["bots"].items()
        for token, owner in self.stores["tokens"].items().items():
            u = users.setdefault(str(owner), new_user())
            meta = bots_meta.get(h(token), {})
            self.data["bots"][token] = {"owner": owner, "username": meta.get("username", "unknown")}
            u["bots"].append(token)
        self.removed = {}  # পুরনো deleted মার্কার চ্যানেল থেকে নিজে মুছে যাবে

    def save(self):
        users = self.data["users"]
        bots = self.data["bots"]
        self.stores["mod"].sync({uid: {"banned": u["banned"]} for uid, u in users.items()})
        self.stores["refer"].sync({uid: {"by": u["referred_by"]}
                                   for uid, u in users.items() if u["referred_by"]})
        self.stores["tokens"].sync({t: b["owner"] for t, b in bots.items()})
        meta = dict(self.removed)
        meta.update({h(t): {"owner": b["owner"], "username": b["username"]} for t, b in bots.items()})
        self.stores["bots"].sync(meta)

    async def flush(self):
        async with self.lock:
            for s in self.stores.values():
                await s.flush(self.bot)

    def user(self, uid):
        return self.data["users"].get(str(uid))

    def add_user(self, uid, referred_by=None):
        u = {"bots": [], "refs": [], "referred_by": referred_by, "banned": False}
        self.data["users"][str(uid)] = u
        self.save()
        return u

    def all_users(self):
        return self.data["users"]

    def bot_exists(self, token):
        return token in self.data["bots"]

    def add_bot(self, uid, token, username):
        self.removed.pop(h(token), None)
        self.data["bots"][token] = {"owner": uid, "username": username}
        self.user(uid)["bots"].append(token)
        self.save()

    def remove_bot(self, uid, token):
        self.data["bots"].pop(token, None)
        u = self.user(uid)
        if u and token in u["bots"]:
            u["bots"].remove(token)
        self.save()

    def limit(self, uid):
        return BOOST_LIMIT if len(self.user(uid)["refs"]) >= REFER_NEED else BASE_LIMIT


db = Storage()

# ================= হেল্পার =================
def h(token):  # webhook path (টোকেন ফাঁস হবে না)
    return hashlib.sha256(token.encode()).hexdigest()[:20]

def secret(token):
    return hashlib.sha256((token + "_s").encode()).hexdigest()[:32]

def banned_text(uid):
    return (f"🚫 <b>You Are Banned</b>\n\n"
            f"আনব্যান হতে হলে এই আইডিটি কপি করে নিচের সাপোর্টে ক্লিক করে পাঠান:\n\n"
            f"🆔 <code>{uid}</code>")

def main_keyboard(uid):
    rows = [[KeyboardButton("🤖 Create Own Bot", style="success"), KeyboardButton("📋 My Bots", style="primary")],
            [KeyboardButton("👥 Refer & Earn", style="primary"), KeyboardButton("💬 Support", style="danger")]]
    if uid == ADMIN_ID:
        rows.append([KeyboardButton("🛠 Admin Panel", style="danger")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def support_inline():
    return InlineKeyboardMarkup([[InlineKeyboardButton("💬 Support", url=SUPPORT_URL)]])


# ================= চাইল্ড বট (অটো রিঅ্যাকশন) =================
APPS = {}  # path_hash -> (Application, token)

async def react(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    log.info("child update: chat=%s type=%s", getattr(msg, "chat_id", None), getattr(getattr(msg, "chat", None), "type", None))
    if not msg:
        return
    if msg.chat.type == "private":
        from html import escape
        me = await context.bot.get_me()
        name = escape(msg.from_user.first_name or "বন্ধু")
        text = (f"✨👋 স্বাগতম, <b>{name}</b>\n"
                "আমি অটো রিয়েকশন বট। আমাকে চ্যানেলে বা গ্রুপে অ্যাড করলে "
                "আমি অটো রিয়েকশন দিয়ে থাকি।\n\n"
                "This bot was made using @SadiaApuBot")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add to Group",
                                  url=f"https://t.me/{me.username}?startgroup=true")],
            [InlineKeyboardButton("💬 Support", url=SUPPORT_URL)]])
        await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)
        return
    emojis = REACTIONS[:]
    random.shuffle(emojis)
    for e in emojis[:3]:
        try:
            await context.bot.set_message_reaction(
                chat_id=msg.chat_id, message_id=msg.message_id,
                reaction=[ReactionTypeEmoji(e)])
            return
        except Exception as ex:
            log.error("react failed (%s): %s", e, ex)

async def start_child(token):
    app = ApplicationBuilder().token(token).updater(None).build()
    app.add_handler(MessageHandler(filters.ALL, react))
    await app.initialize()
    await app.start()
    await app.bot.set_webhook(f"{BASE_URL}/hook/{h(token)}", secret_token=secret(token),
                              allowed_updates=["message", "channel_post"])
    APPS[h(token)] = (app, token)

async def stop_child(token):
    item = APPS.pop(h(token), None)
    if not item:
        return
    app = item[0]
    try:
        await app.bot.delete_webhook()
        await app.stop()
        await app.shutdown()
    except Exception as e:
        log.warning("stop child: %s", e)


# ================= মেইন বট =================
async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id == ADMIN_ID:
        return
    u = db.user(user.id)
    if u and u["banned"]:
        if update.callback_query:
            await update.callback_query.answer()
        await context.bot.send_message(user.id, banned_text(user.id),
                                       parse_mode="HTML", reply_markup=support_inline())
        raise ApplicationHandlerStop

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    context.user_data.clear()
    if not db.user(uid):
        ref = None
        if context.args and context.args[0].startswith("ref_"):
            try:
                ref = int(context.args[0][4:])
            except ValueError:
                ref = None
        db.add_user(uid, ref if ref and ref != uid and db.user(ref) else None)
        r = db.user(uid)["referred_by"]
        if r:
            ru = db.user(r)
            if uid not in ru["refs"]:
                ru["refs"].append(uid)
                db.save()
                try:
                    n = len(ru["refs"])
                    text = (f"🎉Congratulations 🎉\n"
                            f"🎉 নতুন একজন তোমার লিংকে জয়েন করেছে! ({min(n, REFER_NEED)}/{REFER_NEED})")
                    if n == REFER_NEED:
                        text += f"\n✅ অভিনন্দন! এখন তুমি মোট {BOOST_LIMIT}টা বট বানাতে পারবে।"
                    await context.bot.send_message(r, text)
                except Exception:
                    pass
    from html import escape
    name = escape(update.effective_user.first_name or "বন্ধু")
    await update.message.reply_text(
        f"✨👋 স্বাগতম, <b>{name}</b>!\n\n"
        "🤖 এখানে তুমি নিজের <b>অটো রিয়েকশন বট</b> বানাতে পারবে, একদম ফ্রি!\n\n"
        "⚡ তোমার বট গ্রুপ ও চ্যানেলের প্রতিটি পোস্টে নিজে থেকে রিয়েকশন দেবে।\n\n"
        "নিচের মেনু থেকে শুরু করো 👇",
        parse_mode="HTML",
        reply_markup=main_keyboard(uid))

async def create_bot_prompt(update, context):
    uid = update.effective_user.id
    u = db.user(uid)
    if len(u["bots"]) >= db.limit(uid):
        await update.message.reply_text(
            f"❌ তোমার বট লিমিট শেষ ({len(u['bots'])}/{db.limit(uid)})।\n"
            f"👥 Refer & Earn থেকে {REFER_NEED} জনকে রেফার করো তাহলে {BOOST_LIMIT} টা বট বানাতে পারবেন")
        return
    context.user_data["state"] = "token"
    text = (
        "🤖 <b>নিজের অটো রিঅ্যাকশন বট বানাও</b>\n\n"
        "<b>১.</b> @BotFather এ ক্লিক করো\n"
        "<b>২.</b> <code>/newbot</code> লিখে পাঠাও (ট্যাপ করলে কপি হবে)\n"
        "<b>৩.</b> বটের <b>নাম</b> দাও, যেমন: <code>Auto Reaction</code>\n"
        "<b>৪.</b> বটের <b>ইউজারনেম</b> দাও, যেমন: <code>reaction37371_bot</code>\n"
        "    ⚠️ ইউজারনেমের শেষে অবশ্যই <code>bot</code> থাকতে হবে\n"
        "<b>৫.</b> BotFather তোমাকে এরকম একটি <b>টোকেন</b> দেবে:\n"
        "<code>123456789:AAExampleTokenHere1234567890abc</code>\n\n"
        "🔑 সেই টোকেনটি কপি করে <b>এখানে পাঠাও</b>")
    await update.message.reply_text(text, parse_mode="HTML")

async def handle_token(update, context):
    uid = update.effective_user.id
    token = update.message.text.strip()
    context.user_data.pop("state", None)
    try:
        await update.message.delete()  # নিরাপত্তার জন্য টোকেন মেসেজ মুছে ফেলা
    except Exception:
        pass
    send = context.bot.send_message
    u = db.user(uid)
    if len(u["bots"]) >= db.limit(uid):
        await send(uid, "❌ তোমার বট লিমিট শেষ। রেফার করো।")
        return
    if db.bot_exists(token):
        await send(uid, "⚠️ এই টোকেন আগেই দেওয়া হয়েছে।")
        return
    try:
        me = await Bot(token).get_me()
    except Exception:
        await send(uid, "❌ টোকেন ভুল। আবার চেষ্টা করো।")
        return
    try:
        await start_child(token)
    except Exception as e:
        log.error("start child: %s", e)
        await send(uid, "❌ বট চালু করা গেল না, আবার চেষ্টা করো।")
        return
    db.add_bot(uid, token, me.username)
    await send(uid, f"✅ @{me.username} চালু হয়ে গেছে!\n"
                    "বটটাকে গ্রুপ/চ্যানেলে অ্যাডমিন বানাও, তাহলে অটো রিঅ্যাকশন দেবে।")

async def my_bots(update, context):
    uid = update.effective_user.id
    u = db.user(uid)
    if not u["bots"]:
        await update.message.reply_text("তোমার কোনো বট নেই।")
        return
    text = f"📋 তোমার বট: {len(u['bots'])}/{db.limit(uid)}"
    rows = []
    for t in u["bots"]:
        un = db.data["bots"][t]["username"]
        rows.append([InlineKeyboardButton(f"@{un}", url=f"https://t.me/{un}"),
                     InlineKeyboardButton("❌ Remove", callback_data=f"rm:{h(t)}", style="danger")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))

async def remove_cb(update, context):
    q = update.callback_query
    uid = q.from_user.id
    ph = q.data.split(":", 1)[1]
    u = db.user(uid)
    token = next((t for t in u["bots"] if h(t) == ph), None)
    if not token:
        await q.answer("বট পাওয়া যায়নি", show_alert=True)
        return
    un = db.data["bots"][token]["username"]
    await stop_child(token)
    db.remove_bot(uid, token)
    await q.answer("মুছে ফেলা হয়েছে")
    await q.edit_message_text(f"🗑 @{un} মুছে ফেলা হয়েছে।")

async def refer(update, context):
    uid = update.effective_user.id
    me = await context.bot.get_me()
    n = len(db.user(uid)["refs"])
    link = f"https://t.me/{me.username}?start=ref_{uid}"
    if n >= REFER_NEED:
        status = f"✅ রেফার সম্পূর্ণ! তোমার লিমিট {BOOST_LIMIT}টা বট।"
    else:
        status = f"রেফার হয়েছে: {n}/{REFER_NEED}"
    from urllib.parse import quote
    from telegram import CopyTextButton
    share_text = ("🤖 নিজের গ্রুপ/চেনেলের জন্য অটো রিঅ্যাকশন বট বানান, একদম ফ্রি! 👇\n\n"
                  f"{link}")
    share_url = f"https://t.me/share/url?url={quote(share_text)}"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📤 Refer Now", url=share_url),
        InlineKeyboardButton("📋 Copy Link", copy_text=CopyTextButton(text=link))]])
    await update.message.reply_text(
        f"👥 {REFER_NEED} জনকে রেফার করো, তাহলে মোট {BOOST_LIMIT}টা বট বানাতে পারবে।\n\n"
        f"🔗 তোমার লিংক:\n{link}\n\n{status}",
        reply_markup=kb)

async def support(update, context):
    await update.message.reply_text("আমাদের সাপোর্ট টিমের সাথে যোগাযোগ করতে নিচের বাটনে ক্লিক করুন:", reply_markup=support_inline())


# ================= অ্যাডমিন প্যানেল =================
def admin_inline():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Users", callback_data="ad:users", style="primary")],
        [InlineKeyboardButton("🚫 Ban", callback_data="ad:ban", style="danger"),
         InlineKeyboardButton("✅ Unban", callback_data="ad:unban", style="success")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="ad:bc", style="primary")]])

async def admin_panel(update, context):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("🛠 Admin Panel", reply_markup=admin_inline())

async def admin_cb(update, context):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID:
        await q.answer()
        return
    act = q.data.split(":")[1]
    await q.answer()
    if act == "users":
        us = db.all_users()
        nb = sum(1 for u in us.values() if u["banned"])
        lines = [f"👤 মোট ইউজার: {len(us)}", f"🚫 ব্যান: {nb}", ""]
        for uid, u in us.items():
            lines.append(f"{uid} — বট: {len(u['bots'])}{' 🚫' if u['banned'] else ''}")
        text = "\n".join(lines)[:4000]
        await q.message.reply_text(text)
    else:
        context.user_data["state"] = {"ban": "ban", "unban": "unban", "bc": "broadcast"}[act]
        prompt = {"ban": "যাকে ব্যান করবে তার আইডি দাও:",
                  "unban": "যার ব্যান তুলবে তার আইডি দাও:",
                  "bc": "ব্রডকাস্টের মেসেজ লেখো:"}[act]
        await q.message.reply_text(prompt)

async def handle_admin_state(update, context, state):
    context.user_data.pop("state", None)
    text = update.message.text.strip()
    if state in ("ban", "unban"):
        try:
            target = int(text)
        except ValueError:
            await update.message.reply_text("❌ সঠিক আইডি দাও।")
            return
        u = db.user(target)
        if not u:
            await update.message.reply_text("❌ এই আইডির ইউজার পাওয়া যায়নি।")
            return
        u["banned"] = (state == "ban")
        if state == "ban":
            for token in list(u["bots"]):
                await stop_child(token)
                db.remove_bot(target, token)
        db.save()
        await update.message.reply_text("🚫 ব্যান করা হয়েছে।" if state == "ban" else "✅ আনব্যান করা হয়েছে।")
    else:
        ok = fail = 0
        await update.message.reply_text("📢 পাঠানো শুরু হচ্ছে...")
        for uid in list(db.all_users()):
            try:
                await context.bot.send_message(int(uid), text)
                ok += 1
            except Exception:
                fail += 1
            await asyncio.sleep(0.05)
        await update.message.reply_text(f"✅ পাঠানো: {ok}\n❌ ব্যর্থ: {fail}")


# ================= টেক্সট রাউটার =================
async def text_router(update, context):
    uid = update.effective_user.id
    text = update.message.text
    if not db.user(uid):
        await start(update, context)
        return
    menu = {"🤖 Create Own Bot": create_bot_prompt, "📋 My Bots": my_bots,
            "👥 Refer & Earn": refer, "💬 Support": support}
    if text in menu:
        context.user_data.pop("state", None)
        await menu[text](update, context)
        return
    if text == "🛠 Admin Panel":
        await admin_panel(update, context)
        return
    state = context.user_data.get("state")
    if state == "token":
        await handle_token(update, context)
    elif state in ("ban", "unban", "broadcast") and uid == ADMIN_ID:
        await handle_admin_state(update, context, state)


# ================= ওয়েব সার্ভার =================
async def webhook(request: web.Request):
    ph = request.match_info["ph"]
    item = APPS.get(ph)
    if not item:
        return web.Response(status=404)
    app, token = item
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret(token):
        return web.Response(status=403)
    data = await request.json()
    await app.update_queue.put(Update.de_json(data, app.bot))
    return web.Response(text="ok")

async def health(request):
    return web.Response(text="alive")

TASKS = []

async def flusher():
    while True:
        await asyncio.sleep(3)
        try:
            await db.flush()
        except Exception as e:
            log.error("flush error: %s", e)

async def on_cleanup(web_app):
    for t in TASKS:
        t.cancel()
    await db.flush()

async def on_startup(web_app):
    main = ApplicationBuilder().token(MAIN_TOKEN).updater(None).build()
    main.add_handler(TypeHandler(Update, gate), group=-1)
    main.add_handler(CommandHandler("start", start, filters.ChatType.PRIVATE))
    main.add_handler(CallbackQueryHandler(remove_cb, pattern="^rm:"))
    main.add_handler(CallbackQueryHandler(admin_cb, pattern="^ad:"))
    main.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, text_router))
    main.add_handler(MessageHandler(~filters.ChatType.PRIVATE, react))
    await main.initialize()
    await db.load(main.bot)
    TASKS.append(asyncio.create_task(flusher()))
    await main.start()
    await main.bot.set_webhook(f"{BASE_URL}/hook/{h(MAIN_TOKEN)}", secret_token=secret(MAIN_TOKEN),
                               allowed_updates=["message", "callback_query", "channel_post"])
    APPS[h(MAIN_TOKEN)] = (main, MAIN_TOKEN)
    # সেভ করা সব ইউজার-বট আবার চালু
    for t in list(db.data["bots"]):
        try:
            await start_child(t)
        except Exception as e:
            log.error("restore failed: %s", e)
    log.info("started, %d child bots", len(db.data["bots"]))

def main():
    web_app = web.Application()
    web_app.router.add_get("/", health)
    web_app.router.add_post("/hook/{ph}", webhook)
    web_app.on_startup.append(on_startup)
    web_app.on_cleanup.append(on_cleanup)
    web.run_app(web_app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()