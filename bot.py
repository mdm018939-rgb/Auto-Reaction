import os, json, random, hashlib, asyncio, logging
from aiohttp import web
from telegram import (Update, Bot, ReplyKeyboardMarkup, KeyboardButton,
                      InlineKeyboardMarkup, InlineKeyboardButton, ReactionTypeEmoji)
from telegram.ext import (ApplicationBuilder, Application, CommandHandler, MessageHandler,
                          CallbackQueryHandler, TypeHandler, ContextTypes, filters,
                          ApplicationHandlerStop)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("motherbot")

# ================= কনফিগ =================
MAIN_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ.get("BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")  # https://xxx.onrender.com
PORT = int(os.environ.get("PORT", 8080))
ADMIN_ID = 6625019627
SUPPORT_URL = "https://t.me/teemcs"
BASE_LIMIT, BOOST_LIMIT, REFER_NEED = 5, 50, 5
REACTIONS = ["👍", "❤", "🔥", "🥰", "👏", "😁", "🎉", "🤩", "💯", "⚡", "🏆", "😍"]
DB_FILE = "data.json"


# ================= স্টোরেজ =================
# পরে এই ক্লাসটা টেলিগ্রাম চ্যানেলে সেভ করার মতো করে বদলাবে
class Storage:
    def __init__(self, path):
        self.path = path
        self.data = {"users": {}, "bots": {}}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.data = json.load(f)

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)

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


db = Storage(DB_FILE)

# ================= হেল্পার =================
def h(token):  # webhook path (টোকেন ফাঁস হবে না)
    return hashlib.sha256(token.encode()).hexdigest()[:20]

def secret(token):
    return hashlib.sha256((token + "_s").encode()).hexdigest()[:32]

def banned_text(uid):
    return f"আপনি ব্যান হয়েছেন। যোগাযোগ করুন @teemcs, আপনার আইডি: {uid}"

def main_keyboard(uid):
    rows = [[KeyboardButton("🤖 Create Own Bot"), KeyboardButton("📋 My Bots")],
            [KeyboardButton("👥 Refer & Earn"), KeyboardButton("🆘 Support")]]
    if uid == ADMIN_ID:
        rows.append([KeyboardButton("🛠 Admin Panel")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def support_inline():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🆘 Support", url=SUPPORT_URL)]])


# ================= চাইল্ড বট (অটো রিঅ্যাকশন) =================
APPS = {}  # path_hash -> (Application, token)

async def react(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    log.info("child update: chat=%s type=%s", getattr(msg, "chat_id", None), getattr(getattr(msg, "chat", None), "type", None))
    if not msg or msg.chat.type == "private":
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
        await context.bot.send_message(user.id, banned_text(user.id))
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
                    text = f"🎉 নতুন একজন তোমার লিংকে জয়েন করেছে! ({min(n, REFER_NEED)}/{REFER_NEED})"
                    if n == REFER_NEED:
                        text += f"\n✅ অভিনন্দন! এখন তুমি মোট {BOOST_LIMIT}টা বট বানাতে পারবে।"
                    await context.bot.send_message(r, text)
                except Exception:
                    pass
    await update.message.reply_text(
        "👋 স্বাগতম! এখানে তুমি নিজের অটো রিঅ্যাকশন বট বানাতে পারবে।",
        reply_markup=support_inline())
    await update.message.reply_text("নিচের মেনু থেকে বেছে নাও 👇", reply_markup=main_keyboard(uid))

async def create_bot_prompt(update, context):
    uid = update.effective_user.id
    u = db.user(uid)
    if len(u["bots"]) >= db.limit(uid):
        await update.message.reply_text(
            f"❌ তোমার বট লিমিট শেষ ({len(u['bots'])}/{db.limit(uid)})।\n"
            f"👥 Refer & Earn থেকে {REFER_NEED} জনকে রেফার করো।")
        return
    context.user_data["state"] = "token"
    await update.message.reply_text("🔑 তোমার বটের টোকেন পাঠাও (@BotFather থেকে পাওয়া):")

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
                     InlineKeyboardButton("❌ Remove", callback_data=f"rm:{h(t)}")])
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
    await update.message.reply_text(
        f"👥 {REFER_NEED} জনকে রেফার করো, তাহলে মোট {BOOST_LIMIT}টা বট বানাতে পারবে।\n\n"
        f"🔗 তোমার লিংক:\n{link}\n\n{status}")

async def support(update, context):
    await update.message.reply_text("সাহায্যের জন্য নিচের বাটনে ক্লিক করো 👇", reply_markup=support_inline())


# ================= অ্যাডমিন প্যানেল =================
def admin_inline():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Users", callback_data="ad:users")],
        [InlineKeyboardButton("🚫 Ban", callback_data="ad:ban"),
         InlineKeyboardButton("✅ Unban", callback_data="ad:unban")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="ad:bc")]])

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
            "👥 Refer & Earn": refer, "🆘 Support": support}
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

async def on_startup(web_app):
    main = ApplicationBuilder().token(MAIN_TOKEN).updater(None).build()
    main.add_handler(TypeHandler(Update, gate), group=-1)
    main.add_handler(CommandHandler("start", start, filters.ChatType.PRIVATE))
    main.add_handler(CallbackQueryHandler(remove_cb, pattern="^rm:"))
    main.add_handler(CallbackQueryHandler(admin_cb, pattern="^ad:"))
    main.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, text_router))
    main.add_handler(MessageHandler(~filters.ChatType.PRIVATE, react))
    await main.initialize()
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
    web.run_app(web_app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
