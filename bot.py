"""Aniwu kolleksiya boti (aiogram 3.x + SQLite)

O'rnatish:   pip install aiogram
Ishga tushirish (Linux/Mac):
    BOT_TOKEN="123:ABC" OWNER_ID=111222333 ADMIN_IDS="444,555" \
    WEBAPP_URL="https://sizning-domen.uz" PORT=8080 python bot.py
WEBAPP_URL - "Ilova" (Mini App) uchun ochiq HTTPS manzil (app.html shu bot orqali beriladi).
OWNER_ID - ega (cheksiz pul, olmos va barcha Aniwular). ADMIN_IDS - qo'shimcha adminlar.
"""
import asyncio
import hashlib
import hmac
import json
import os
import random
import sqlite3
from datetime import date
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, KeyboardButton, MenuButtonWebApp, Message,
                           ReplyKeyboardMarkup, WebAppInfo)
from aiogram.types import InlineKeyboardButton as Btn
from aiogram.types import InlineKeyboardMarkup as Kb

TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
ADMINS = {OWNER_ID} | {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}

# daraja: (belgi, tushish vazni) - vazn kichik bo'lsa, tushishi qiyin
RARITY = {
    "common": ("🔵", 60),
    "epic": ("🟣", 25),
    "legendary": ("🟠", 10),
    "mifik": ("🔴", 4),
    "ultimate": ("🟡", 1),
}
FREE_BOXES_PER_DAY = 3
GIFTS_PER_DAY = 3
INVITES_PER_BOX = 10
MONEY_PER_DIAMOND = 1000
BOX_PRICE = 500  # tekin sandiqlar tugagach, har bir sandiq narxi
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
PORT = int(os.getenv("PORT", "8080"))

bot = Bot(TOKEN)
dp = Dispatcher()

db = sqlite3.connect(os.getenv("DB_PATH", "aniwu.db"))
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT, name TEXT,
  money INTEGER DEFAULT 0, diamonds INTEGER DEFAULT 0,
  box_date TEXT DEFAULT '', box_used INTEGER DEFAULT 0, extra_boxes INTEGER DEFAULT 0,
  gift_date TEXT DEFAULT '', gift_used INTEGER DEFAULT 0, invites INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS collections(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE, reward INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS aniwus(id INTEGER PRIMARY KEY, col_id INTEGER, name TEXT, rarity TEXT, price INTEGER, photo TEXT);
CREATE TABLE IF NOT EXISTS inv(uid INTEGER, aid INTEGER, qty INTEGER, PRIMARY KEY(uid, aid));
CREATE TABLE IF NOT EXISTS market(id INTEGER PRIMARY KEY AUTOINCREMENT, seller INTEGER, aid INTEGER, price INTEGER);
CREATE TABLE IF NOT EXISTS claimed(uid INTEGER, col_id INTEGER, PRIMARY KEY(uid, col_id));
CREATE TABLE IF NOT EXISTS stickers(key TEXT PRIMARY KEY, file_id TEXT);
CREATE TABLE IF NOT EXISTS game(id INTEGER PRIMARY KEY, aid INTEGER);
CREATE TABLE IF NOT EXISTS chats(id INTEGER PRIMARY KEY, title TEXT DEFAULT '', kind TEXT DEFAULT 'group');
""")


# ---------- yordamchi funksiyalar ----------
def q(sql, *a):
    return db.execute(sql, a).fetchall()


def q1(sql, *a):
    return db.execute(sql, a).fetchone()


def ex(sql, *a):
    cur = db.execute(sql, a)
    db.commit()
    return cur


def is_owner(uid):
    return uid == OWNER_ID

def is_admin(uid):
    return uid in ADMINS


def find_user(ref):
    ref = ref.strip()
    if ref.isdigit():
        return q1("SELECT * FROM users WHERE id=?", int(ref))
    return q1("SELECT * FROM users WHERE username=?", ref.lstrip("@").lower())


def get_aniwu(aid):
    return q1("SELECT * FROM aniwus WHERE id=?", aid)


def label(a):
    return f"{RARITY[a['rarity']][0]} #{a['id']} {a['name']}"


def card(a):
    return f"{label(a)}\nDaraja: {a['rarity'].upper()}\nNarx: {a['price']:,} 💰"


def balance(uid, col):
    return q1(f"SELECT {col} FROM users WHERE id=?", uid)[col]


def has_bal(uid, col, n):
    return is_owner(uid) or balance(uid, col) >= n


def change_bal(uid, col, n):  # col: "money" yoki "diamonds"; ega uchun o'zgarmaydi (cheksiz)
    if not is_owner(uid):
        ex(f"UPDATE users SET {col}={col}+? WHERE id=?", n, uid)


def qty(uid, aid):
    if is_owner(uid):
        return 10**9
    r = q1("SELECT qty FROM inv WHERE uid=? AND aid=?", uid, aid)
    return r["qty"] if r else 0


def take(uid, aid, n=1):
    if not is_owner(uid):
        ex("UPDATE inv SET qty=qty-? WHERE uid=? AND aid=?", n, uid, aid)
        ex("DELETE FROM inv WHERE qty<=0")


async def give(uid, aid, n=1):
    if not is_owner(uid):
        ex("INSERT INTO inv VALUES(?,?,?) ON CONFLICT(uid,aid) DO UPDATE SET qty=qty+?", uid, aid, n, n)
        await check_collections(uid)


async def safe_send(uid, text, **kw):
    try:
        return await bot.send_message(uid, text, **kw)
    except Exception:
        return None


async def sticker(uid, key):
    r = q1("SELECT file_id FROM stickers WHERE key=?", key)
    if r:
        try:
            await bot.send_sticker(uid, r["file_id"])
        except Exception:
            pass


async def check_collections(uid):
    for c in q("SELECT * FROM collections WHERE id NOT IN (SELECT col_id FROM claimed WHERE uid=?)", uid):
        total = q1("SELECT COUNT(*) n FROM aniwus WHERE col_id=?", c["id"])["n"]
        have = q1("SELECT COUNT(*) n FROM inv i JOIN aniwus a ON a.id=i.aid WHERE i.uid=? AND a.col_id=?",
                  uid, c["id"])["n"]
        if total and have == total:
            ex("INSERT INTO claimed VALUES(?,?)", uid, c["id"])
            change_bal(uid, "diamonds", c["reward"])
            await sticker(uid, "toplam")
            await safe_send(uid, f"🏆 \"{c['name']}\" to'plamini to'liq yig'dingiz!\n+{c['reward']} 💎 olmos")


async def broadcast(fn):
    ok = 0
    targets = [r["id"] for r in q("SELECT id FROM users")] + [r["id"] for r in q("SELECT id FROM chats")]
    for chat_id in dict.fromkeys(targets):
        try:
            await fn(chat_id)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    return ok


def pick_aniwu():
    rows = q("SELECT * FROM aniwus")
    if not rows:
        return None
    rar = random.choices(list(RARITY), weights=[v[1] for v in RARITY.values()])[0]
    return random.choice([r for r in rows if r["rarity"] == rar] or rows)


def nums(command, count):
    a = (command.args or "").split()
    return [int(x) for x in a] if len(a) == count and all(x.isdigit() for x in a) else None


async def reg(handler, event, data):
    u = data.get("event_from_user")
    data["is_new"] = False
    chat = data.get("event_chat")
    if chat and chat.type in ("group", "supergroup"):
        title = getattr(chat, "title", "") or ""
        ex("INSERT INTO chats(id,title,kind) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title",
           chat.id, title, chat.type)
    if u and not u.is_bot:
        data["is_new"] = q1("SELECT 1 FROM users WHERE id=?", u.id) is None
        ex("INSERT INTO users(id,username,name) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET "
           "username=excluded.username, name=excluded.name", u.id, (u.username or "").lower(), u.full_name)
    return await handler(event, data)


dp.update.outer_middleware(reg)

HELP = """🎴 Aniwu kolleksiya botiga xush kelibsiz!

👤 /profil - hisobingiz
🎁 /sandiq - kuniga 3 ta tekin, keyin 500 💰
📚 /toplamlar, /toplam <id>
🔍 /aniwu <raqam> - Aniwu rasmi va narxi
🎒 /mening - mening Aniwularim
💝 /sovga <raqam> <@user|id> - kuniga 3 ta
🔄 /almashtir <mening> <uniki> <@user|id>
🏪 /market, /sotish <raqam> <narx>, /olib_tashla <market id>
⬆️ /upgrade <mening> <maqsad>
💎 /olmos <son> - 1000 💰 = 1 💎\n/olmos_pul <son> - 1 💎 = 1000 💰"""


# ---------- foydalanuvchi buyruqlari ----------
@dp.message(CommandStart())
async def start(m: Message, command: CommandObject, is_new: bool):
    arg = command.args or ""
    if is_new and arg.startswith("ref_") and arg[4:].isdigit():
        inviter = int(arg[4:])
        if inviter != m.from_user.id and q1("SELECT 1 FROM users WHERE id=?", inviter):
            ex("UPDATE users SET invites=invites+1 WHERE id=?", inviter)
            if balance(inviter, "invites") % INVITES_PER_BOX == 0:
                ex("UPDATE users SET extra_boxes=extra_boxes+1 WHERE id=?", inviter)
                await safe_send(inviter, f"🎁 {INVITES_PER_BOX} ta do'st taklif qildingiz! +1 tekin sandiq")
    me = await bot.me()
    await m.answer(HELP + f"\n\n🔗 Taklif havolangiz ({INVITES_PER_BOX} ta do'st = 1 sandiq):\n"
                          f"https://t.me/{me.username}?start=ref_{m.from_user.id}",
                   reply_markup=menu(m.from_user.id))


@dp.message(Command("profil"))
async def profil(m: Message):
    uid = m.from_user.id
    u = q1("SELECT * FROM users WHERE id=?", uid)
    own = is_owner(uid)
    used = u["box_used"] if u["box_date"] == str(date.today()) else 0
    cnt = q1("SELECT COALESCE(SUM(qty),0) n, COUNT(*) k FROM inv WHERE uid=?", uid)
    await m.answer(
        f"👤 {u['name']}\n💰 Pul: {'∞' if own else format(u['money'], ',')}\n"
        f"💎 Olmos: {'∞' if own else u['diamonds']}\n"
        f"🎴 Aniwular: {'∞' if own else cnt['n']} ({'hammasi' if own else str(cnt['k']) + ' xil'})\n"
        f"🎁 Tekin sandiq: {max(FREE_BOXES_PER_DAY - used, 0)} + {u['extra_boxes']} (bonus)\n"
        f"👥 Takliflar: {u['invites']}")


@dp.message(Command("olmos_pul"))
async def olmos_pul(m: Message, command: CommandObject):
    n = nums(command, 1)
    if not n or n[0] < 1:
        return await m.answer(f"Foydalanish: /olmos_pul <son>\n1 💎 = {MONEY_PER_DIAMOND} 💰")
    uid = m.from_user.id
    if not has_bal(uid, "diamonds", n[0]):
        return await m.answer("❌ Olmos yetarli emas.")
    change_bal(uid, "diamonds", -n[0])
    change_bal(uid, "money", n[0] * MONEY_PER_DIAMOND)
    await m.answer(f"✅ {n[0]} 💎 olmos {n[0] * MONEY_PER_DIAMOND:,} 💰 pulga aylantirildi!")


@dp.message(Command("olmos"))
async def olmos(m: Message, command: CommandObject):
    n = nums(command, 1)
    if not n or n[0] < 1:
        return await m.answer(f"Foydalanish: /olmos <son>\n{MONEY_PER_DIAMOND} 💰 = 1 💎")
    uid = m.from_user.id
    if not has_bal(uid, "money", n[0] * MONEY_PER_DIAMOND):
        return await m.answer("❌ Pul yetarli emas.")
    change_bal(uid, "money", -n[0] * MONEY_PER_DIAMOND)
    change_bal(uid, "diamonds", n[0])
    await m.answer(f"✅ {n[0]} 💎 olmos oldingiz!")


def open_box(uid):
    """(Aniwu, izoh) yoki (None, xato matni) qaytaradi."""
    a = pick_aniwu()
    if not a:
        return None, "Hozircha Aniwu yo'q."
    note = ""
    if not is_owner(uid):
        u = q1("SELECT * FROM users WHERE id=?", uid)
        today = str(date.today())
        used = u["box_used"] if u["box_date"] == today else 0
        if used < FREE_BOXES_PER_DAY:
            ex("UPDATE users SET box_date=?, box_used=? WHERE id=?", today, used + 1, uid)
            note = "🎁 Tekin sandiq"
        elif u["extra_boxes"] > 0:
            ex("UPDATE users SET extra_boxes=extra_boxes-1 WHERE id=?", uid)
            note = "🎁 Bonus sandiq"
        elif u["money"] >= BOX_PRICE:
            change_bal(uid, "money", -BOX_PRICE)
            note = f"📦 Sandiq ochildi: -{BOX_PRICE} 💰"
        else:
            return None, f"❌ Tekin sandiqlar tugadi. Yana ochish {BOX_PRICE} 💰, pulingiz yetmaydi."
    return a, note


@dp.message(Command("sandiq"))
async def sandiq(m: Message):
    uid = m.from_user.id
    a, note = open_box(uid)
    if not a:
        return await m.answer(note)
    await sticker(uid, "sandiq")
    await m.answer_photo(a["photo"], caption=f"{note}\n\n" + card(a))
    if a["rarity"] == "ultimate":
        await sticker(uid, "ultimate")
    await give(uid, a["id"])


@dp.message(Command("aniwu"))
async def aniwu_info(m: Message, command: CommandObject):
    n = nums(command, 1)
    a = get_aniwu(n[0]) if n else None
    if not a:
        return await m.answer("Foydalanish: /aniwu <raqam> (mavjud raqam)")
    have = "∞" if is_owner(m.from_user.id) else qty(m.from_user.id, a["id"])
    await m.answer_photo(a["photo"], caption=card(a) + f"\nSizda: {have} ta")


@dp.message(Command("mening"))
async def mening(m: Message):
    if is_owner(m.from_user.id):
        return await m.answer("👑 Siz egasiz: barcha Aniwular sizda cheksiz!")
    rows = q("SELECT a.*, i.qty FROM inv i JOIN aniwus a ON a.id=i.aid WHERE i.uid=? ORDER BY a.id",
             m.from_user.id)
    if not rows:
        return await m.answer("Sizda hali Aniwu yo'q. /sandiq ni sinang!")
    await m.answer("🎒 Aniwularim:\n" + "\n".join(f"{label(r)} x{r['qty']}" for r in rows[:80]))


@dp.message(Command("toplamlar"))
async def toplamlar(m: Message):
    rows = q("SELECT * FROM collections")
    if not rows:
        return await m.answer("Hozircha to'plam yo'q.")
    out = []
    for c in rows:
        total = q1("SELECT COUNT(*) n FROM aniwus WHERE col_id=?", c["id"])["n"]
        have = q1("SELECT COUNT(*) n FROM inv i JOIN aniwus a ON a.id=i.aid WHERE i.uid=? AND a.col_id=?",
                  m.from_user.id, c["id"])["n"]
        out.append(f"📚 {c['id']}. {c['name']} - {have}/{total} (mukofot: {c['reward']} 💎)")
    await m.answer("\n".join(out) + "\n\nBatafsil: /toplam <id>")


@dp.message(Command("toplam"))
async def toplam(m: Message, command: CommandObject):
    n = nums(command, 1)
    c = q1("SELECT * FROM collections WHERE id=?", n[0]) if n else None
    if not c:
        return await m.answer("Foydalanish: /toplam <id>")
    rows = q("SELECT * FROM aniwus WHERE col_id=? ORDER BY id", c["id"])
    lines = [f"{'✅' if qty(m.from_user.id, r['id']) else '▫️'} {label(r)} - {r['price']:,} 💰" for r in rows]
    await m.answer(f"📚 {c['name']} (mukofot: {c['reward']} 💎)\n\n" + ("\n".join(lines) or "Bo'sh"))


@dp.message(Command("sovga"))
async def sovga(m: Message, command: CommandObject):
    a = (command.args or "").split()
    if len(a) != 2 or not a[0].isdigit():
        return await m.answer("Foydalanish: /sovga <aniwu raqami> <@username yoki id>")
    uid, aid = m.from_user.id, int(a[0])
    to = find_user(a[1])
    if not to or to["id"] == uid:
        return await m.answer("❌ Foydalanuvchi topilmadi (u botni /start qilgan bo'lishi kerak).")
    if not get_aniwu(aid) or qty(uid, aid) < 1:
        return await m.answer("❌ Sizda bu Aniwu yo'q.")
    today = str(date.today())
    u = q1("SELECT * FROM users WHERE id=?", uid)
    used = u["gift_used"] if u["gift_date"] == today else 0
    if not is_owner(uid):
        if used >= GIFTS_PER_DAY:
            return await m.answer(f"❌ Kuniga {GIFTS_PER_DAY} tagacha sovga qilish mumkin.")
        ex("UPDATE users SET gift_date=?, gift_used=? WHERE id=?", today, used + 1, uid)
    take(uid, aid)
    await give(to["id"], aid)
    await m.answer("💝 Sovga yuborildi!")
    await safe_send(to["id"], f"💝 {m.from_user.full_name} sizga Aniwu sovg'a qildi:\n{label(get_aniwu(aid))}")


@dp.message(Command("almashtir"))
async def almashtir(m: Message, command: CommandObject):
    a = (command.args or "").split()
    if len(a) != 3 or not (a[0].isdigit() and a[1].isdigit()):
        return await m.answer("Foydalanish: /almashtir <mening raqamim> <uning raqami> <@user|id>")
    uid, mine, theirs = m.from_user.id, get_aniwu(int(a[0])), get_aniwu(int(a[1]))
    to = find_user(a[2])
    if not (mine and theirs and to) or to["id"] == uid:
        return await m.answer("❌ Ma'lumot noto'g'ri yoki foydalanuvchi topilmadi.")
    if qty(uid, mine["id"]) < 1 or qty(to["id"], theirs["id"]) < 1:
        return await m.answer("❌ Aniwu ikkalangizdan birida yo'q.")
    kb = Kb(inline_keyboard=[[Btn(text="✅ Qabul", callback_data=f"tr:{uid}:{mine['id']}:{theirs['id']}"),
                              Btn(text="❌ Rad", callback_data="tr:no")]])
    sent = await safe_send(to["id"], f"🔄 {m.from_user.full_name} almashuv taklif qilmoqda:\n"
                                     f"U beradi: {label(mine)}\nSiz berasiz: {label(theirs)}", reply_markup=kb)
    await m.answer("✅ Taklif yuborildi." if sent else "❌ Foydalanuvchiga xabar yuborib bo'lmadi.")


@dp.callback_query(F.data.startswith("tr:"))
async def trade_cb(cb: CallbackQuery):
    p = cb.data.split(":")
    if p[1] == "no":
        return await cb.message.edit_text("❌ Almashuv rad etildi.")
    frm, mine, theirs = int(p[1]), int(p[2]), int(p[3])
    me = cb.from_user.id
    if qty(frm, mine) < 1 or qty(me, theirs) < 1:
        return await cb.message.edit_text("⚠️ Almashuv bekor: Aniwu yetarli emas.")
    take(frm, mine)
    take(me, theirs)
    await give(me, mine)
    await give(frm, theirs)
    await cb.message.edit_text("✅ Almashuv bajarildi!")
    await safe_send(frm, "✅ Almashuv taklifingiz qabul qilindi!")


# ---------- market ----------
@dp.message(Command("sotish"))
async def sotish(m: Message, command: CommandObject):
    n = nums(command, 2)
    if not n or n[1] < 1 or not get_aniwu(n[0]):
        return await m.answer("Foydalanish: /sotish <aniwu raqami> <narx>")
    uid, aid, price = m.from_user.id, n[0], n[1]
    if uid not in ADMINS:  # adminlar o'z zaxirasisiz ham qo'ya oladi
        if qty(uid, aid) < 1:
            return await m.answer("❌ Sizda bu Aniwu yo'q.")
        take(uid, aid)
    cur = ex("INSERT INTO market(seller,aid,price) VALUES(?,?,?)", uid, aid, price)
    await m.answer(f"🏪 Marketga qo'yildi (market #{cur.lastrowid}) - {price:,} 💰")


@dp.message(Command("olib_tashla"))
async def olib_tashla(m: Message, command: CommandObject):
    n = nums(command, 1)
    it = q1("SELECT * FROM market WHERE id=?", n[0]) if n else None
    if not it or (it["seller"] != m.from_user.id and m.from_user.id not in ADMINS):
        return await m.answer("❌ Topilmadi yoki bu sizning e'loningiz emas.")
    ex("DELETE FROM market WHERE id=?", it["id"])
    if it["seller"] not in ADMINS:
        await give(it["seller"], it["aid"])
    await m.answer("✅ Marketdan olib tashlandi.")


@dp.message(Command("market"))
async def market(m: Message, command: CommandObject):
    page = int(command.args) if command.args and command.args.isdigit() and int(command.args) > 0 else 1
    rows = q("SELECT m.id mid, m.price mprice, a.id aid, a.name, a.rarity FROM market m "
             "JOIN aniwus a ON a.id=m.aid ORDER BY m.id DESC LIMIT 8 OFFSET ?", (page - 1) * 8)
    if not rows:
        return await m.answer("🏪 Market hozircha bo'sh.")
    kb = Kb(inline_keyboard=[[Btn(
        text=f"🛒 {RARITY[r['rarity']][0]} #{r['aid']} {r['name']} - {r['mprice']:,} 💰",
        callback_data=f"buy:{r['mid']}")] for r in rows])
    await m.answer(f"🏪 Aniwu market (sahifa {page})\nSotib olish uchun tugmani bosing.\n"
                   f"Rasm ko'rish: /aniwu <raqam> | Keyingi: /market {page + 1}", reply_markup=kb)


async def do_buy(uid, mid):
    """(ok, xabar) qaytaradi."""
    it = q1("SELECT * FROM market WHERE id=?", mid)
    if not it:
        return False, "Allaqachon sotilgan yoki olib tashlangan."
    if it["seller"] == uid:
        return False, "Bu sizning e'loningiz."
    if not has_bal(uid, "money", it["price"]):
        return False, "❌ Pul yetarli emas."
    ex("DELETE FROM market WHERE id=?", it["id"])
    change_bal(uid, "money", -it["price"])
    change_bal(it["seller"], "money", it["price"])
    await give(uid, it["aid"])
    await safe_send(it["seller"], f"💰 {label(get_aniwu(it['aid']))} sotildi! +{it['price']:,} 💰")
    return True, "✅ Sotib olindi!"


@dp.callback_query(F.data.startswith("buy:"))
async def buy(cb: CallbackQuery):
    ok, msg = await do_buy(cb.from_user.id, int(cb.data[4:]))
    await cb.answer(msg, show_alert=True)


# ---------- upgrade ----------
def up_chance(mine, tgt):
    return round(min(95, max(1, mine["price"] / tgt["price"] * 100)), 1)


@dp.message(Command("upgrade"))
async def upgrade(m: Message, command: CommandObject):
    n = nums(command, 2)
    mine, tgt = (get_aniwu(n[0]), get_aniwu(n[1])) if n else (None, None)
    if not mine or not tgt or mine["id"] == tgt["id"]:
        return await m.answer("Foydalanish: /upgrade <mening raqamim> <maqsad raqami>")
    if qty(m.from_user.id, mine["id"]) < 1:
        return await m.answer("❌ Sizda bu Aniwu yo'q.")
    ch = up_chance(mine, tgt)
    kb = Kb(inline_keyboard=[[Btn(text=f"⬆️ Boshlash ({ch}%)", callback_data=f"up:{mine['id']}:{tgt['id']}")]])
    await m.answer(f"{label(mine)} ➜ {label(tgt)}\nOmad ehtimoli: {ch}%\n"
                   f"⚠️ Omadsiz bo'lsa {mine['name']} yo'qoladi.", reply_markup=kb)


@dp.callback_query(F.data.startswith("up:"))
async def upgrade_go(cb: CallbackQuery):
    uid = cb.from_user.id
    mine, tgt = get_aniwu(int(cb.data.split(":")[1])), get_aniwu(int(cb.data.split(":")[2]))
    if not mine or not tgt or qty(uid, mine["id"]) < 1:
        return await cb.answer("Sizda bu Aniwu yo'q.", show_alert=True)
    await cb.message.edit_reply_markup(reply_markup=None)
    take(uid, mine["id"])
    if random.random() * 100 < up_chance(mine, tgt):
        await give(uid, tgt["id"])
        await sticker(uid, "yutuq")
        await cb.message.answer_photo(tgt["photo"], caption="🎉 Upgrade muvaffaqiyatli!\n\n" + card(tgt))
    else:
        await cb.message.answer(f"💥 Omad kelmadi, {mine['name']} yo'qoldi.")


# ---------- menyu (rolga qarab: a'zo / admin / ega) ----------
MEMBER_ROWS = [["🎁 Sandiq ochish"], ["👤 Profil", "🎒 Aniwularim"], ["📚 To'plamlar", "🏪 Market"],
               ["⬆️ Upgrade", "🔄 Almashtirish"], ["💝 Sovga", "💎 Olmos"], ["👥 Do'st taklif qilish"]]
ADMIN_ROWS = [["🛠 Admin panel", "➕ Aniwu qo'shish"], ["📢 Xabar yuborish", "🎮 O'yin boshlash"]]
OWNER_ROWS = [["👑 Ega paneli", "📊 Statistika"], ["➕ Admin qoʻshish", "➖ Admin olish"]]
MARKET_ROWS = [["🏪 Aniwu market", "💱 Valyuta"], ["⬅️ Asosiy menyu"]]
flat = lambda rows: {b for r in rows for b in r}
ADMIN_BTNS, OWNER_BTNS = flat(ADMIN_ROWS), flat(OWNER_ROWS)
ALL_BTNS = flat(MEMBER_ROWS) | ADMIN_BTNS | OWNER_BTNS | flat(MARKET_ROWS)
HINTS = {
    "⬆️ Upgrade": "⬆️ /upgrade <mening raqamim> <maqsad raqami>\nOmad ehtimoli narxga bog'liq.",
    "🔄 Almashtirish": "🔄 /almashtir <mening raqamim> <uniki> <@user|id>",
    "💝 Sovga": f"💝 /sovga <aniwu raqami> <@user|id>\nKuniga {GIFTS_PER_DAY} ta.",
    "💎 Olmos": f"💎 /olmos <son>\n{MONEY_PER_DIAMOND} 💰 = 1 💎\nOlmosni pulga: /olmos_pul <son>",
    "➕ Aniwu qo'shish": "1) /toplam_qosh Naruto | 50\n2) Rasm yuboring, izohiga:\n/add 1 | 101 | Naruto | epic | 5000",
    "📢 Xabar yuborish": "📢 /xabar <matn>\nyoki istalgan xabarga reply qilib /xabar",
    "🎮 O'yin boshlash": "🎮 /oyin <aniwu raqami>",
}


def menu(uid):
    rows = [[KeyboardButton(text="📱 Ilova", web_app=WebAppInfo(url=WEBAPP_URL))]] if WEBAPP_URL else []
    extra = (ADMIN_ROWS if uid in ADMINS else []) + (OWNER_ROWS if is_owner(uid) else [])
    rows += [[KeyboardButton(text=b) for b in r] for r in MEMBER_ROWS + extra]
    rows += [[KeyboardButton(text="🧭 Boʻlimlar")]]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def stats():
    return {t: q1(f"SELECT COUNT(*) n FROM {t}")["n"] for t in ("users", "aniwus", "collections", "market")}


@dp.message(Command("menu"))
async def show_menu(m: Message):
    await m.answer("📋 Menyu yangilandi.", reply_markup=menu(m.from_user.id))


@dp.message(F.text.in_(ALL_BTNS))
async def on_menu(m: Message):
    t, uid = m.text, m.from_user.id
    if (t in ADMIN_BTNS and uid not in ADMINS) or (t in OWNER_BTNS and not is_owner(uid)):
        return
    if t == "🎁 Sandiq ochish":
        return await sandiq(m)
    if t == "👤 Profil":
        return await profil(m)
    if t == "🎒 Aniwularim":
        return await mening(m)
    if t == "📚 To'plamlar":
        return await toplamlar(m)
    if t == "🏪 Market":
        return await m.answer("🏪 Market bo‘limi", reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=b) for b in r] for r in MARKET_ROWS], resize_keyboard=True))
    if t == "🏪 Aniwu market":
        return await market(m, CommandObject())
    if t == "💱 Valyuta":
        return await m.answer(f"💱 Valyuta ayirboshlash\nPul → olmos: /olmos <son> ( {MONEY_PER_DIAMOND} 💰 = 1 💎 )\nOlmos → pul: /olmos_pul <son>")
    if t == "⬅️ Asosiy menyu" or t == "🧭 Boʻlimlar":
        return await m.answer("📋 Asosiy menyu", reply_markup=menu(uid))
    if t == "➕ Admin qoʻshish":
        return await m.answer("Foydalanish: /admin_qosh <Telegram ID>")
    if t == "➖ Admin olish":
        return await m.answer("Foydalanish: /admin_ol <Telegram ID>")
    if t == "👥 Do'st taklif qilish":
        me = await bot.me()
        return await m.answer(f"👥 {INVITES_PER_BOX} ta do'st = 1 tekin sandiq\nHavolangiz:\n"
                              f"https://t.me/{me.username}?start=ref_{uid}")
    if t == "🛠 Admin panel":
        return await m.answer(ADMIN_HELP)
    if t == "👑 Ega paneli":
        return await m.answer(ADMIN_HELP + "\n\n👑 Ega: pul, olmos va barcha Aniwular sizda cheksiz.")
    if t == "📊 Statistika":
        s = stats()
        return await m.answer(f"📊 Statistika\n👥 A'zolar: {s['users']}\n🎴 Aniwular: {s['aniwus']}\n"
                              f"📚 To'plamlar: {s['collections']}\n🏪 Marketda: {s['market']}")
    await m.answer(HINTS[t])


@dp.message(Command("admin_qosh"))
async def add_admin(m: Message, command: CommandObject):
    if not is_owner(m.from_user.id):
        return await m.answer("❌ Faqat bot egasi admin qo‘sha oladi.")
    a = nums(command, 1)
    if not a:
        return await m.answer("Foydalanish: /admin_qosh <Telegram ID>")
    ADMINS.add(a[0])
    await m.answer(f"✅ {a[0]} adminlar ro‘yxatiga qo‘shildi. Botni qayta ishga tushirganda saqlanishi uchun ADMIN_IDS muhit o‘zgaruvchisiga ham kiriting.")


@dp.message(Command("admin_ol"))
async def remove_admin(m: Message, command: CommandObject):
    if not is_owner(m.from_user.id):
        return await m.answer("❌ Faqat bot egasi admin huquqini olib tashlay oladi.")
    a = nums(command, 1)
    if not a:
        return await m.answer("Foydalanish: /admin_ol <Telegram ID>")
    if a[0] == OWNER_ID:
        return await m.answer("❌ Egani adminlar ro‘yxatidan olib bo‘lmaydi.")
    ADMINS.discard(a[0])
    await m.answer(f"✅ {a[0]} adminlar ro‘yxatidan olindi. Botni qayta ishga tushirganda saqlanishi uchun ADMIN_IDS ni ham yangilang.")


# ---------- o'yin: birinchi topgan yutadi ----------
@dp.message(F.text & ~F.text.startswith("/"))
async def guess(m: Message):
    g = q1("SELECT * FROM game WHERE id=1")
    a = get_aniwu(g["aid"]) if g else None
    if not a:
        return
    t = m.text.lower()
    if a["name"].lower() in t and a["rarity"] in t:
        ex("DELETE FROM game WHERE id=1")
        await give(m.from_user.id, a["id"])
        await sticker(m.from_user.id, "yutuq")
        await broadcast(lambda uid: bot.send_message(
            uid, f"🏆 {m.from_user.full_name} birinchi topdi!\nJavob: {label(a)} ({a['rarity']})"))
        await m.answer_photo(a["photo"], caption="🎁 Yutuq sizniki!\n\n" + card(a))


# ---------- admin buyruqlari ----------
ADMIN_HELP = """🛠 Admin buyruqlari:
/toplam_qosh <nom> | <olmos mukofoti>
Rasm + izoh: /add <toplam id> | <raqam> | <nom> | <daraja> | <narx>
  (daraja: ultimate, mifik, legendary, epic, common)
/ochir <aniwu raqami>
/ber <@user|id> pul|olmos <son>
/ber <@user|id> aniwu <raqam> [son]
/hammaga pul|olmos|aniwu <son|raqam>
/xabar <matn> (yoki xabarga reply qilib /xabar)
/oyin <aniwu raqami> - rasm yuboriladi, birinchi topgan yutadi
/stiker <kalit> (stikerga reply): sandiq, ultimate, toplam, yutuq\n/admin_qosh <Telegram ID> (faqat ega)\n/admin_ol <Telegram ID> (faqat ega)"""


def admin_only(m: Message):
    return m.from_user.id in ADMINS


@dp.message(Command("admin"), admin_only)
async def admin_help(m: Message):
    await m.answer(ADMIN_HELP)


@dp.message(Command("toplam_qosh"), admin_only)
async def col_add(m: Message, command: CommandObject):
    p = [x.strip() for x in (command.args or "").split("|")]
    if len(p) != 2 or not p[1].isdigit() or not p[0]:
        return await m.answer("Foydalanish: /toplam_qosh Naruto | 50")
    try:
        cur = ex("INSERT INTO collections(name,reward) VALUES(?,?)", p[0], int(p[1]))
    except sqlite3.IntegrityError:
        return await m.answer("❌ Bunday nomli to'plam bor.")
    await m.answer(f"✅ To'plam #{cur.lastrowid} \"{p[0]}\" ochildi (mukofot {p[1]} 💎).")


@dp.message(F.photo, F.caption.startswith("/add"), admin_only)
async def aniwu_add(m: Message):
    p = [x.strip() for x in m.caption[4:].split("|")]
    if len(p) != 5 or not (p[0].isdigit() and p[1].isdigit() and p[4].isdigit()):
        return await m.answer("Foydalanish (rasm izohida):\n/add <toplam id> | <raqam> | <nom> | <daraja> | <narx>")
    col, aid, name, rar, price = int(p[0]), int(p[1]), p[2], p[3].lower(), int(p[4])
    if not q1("SELECT 1 FROM collections WHERE id=?", col):
        return await m.answer("❌ To'plam topilmadi.")
    if rar not in RARITY or price < 1 or get_aniwu(aid):
        return await m.answer("❌ Daraja noto'g'ri, narx 0, yoki bu raqam band.")
    ex("INSERT INTO aniwus VALUES(?,?,?,?,?,?)", aid, col, name, rar, price, m.photo[-1].file_id)
    await m.answer_photo(m.photo[-1].file_id, caption="✅ Qo'shildi:\n\n" + card(get_aniwu(aid)))


@dp.message(Command("ochir"), admin_only)
async def aniwu_del(m: Message, command: CommandObject):
    n = nums(command, 1)
    if not n or not get_aniwu(n[0]):
        return await m.answer("Foydalanish: /ochir <aniwu raqami>")
    for t, c in (("aniwus", "id"), ("inv", "aid"), ("market", "aid")):
        ex(f"DELETE FROM {t} WHERE {c}=?", n[0])
    await m.answer("🗑 O'chirildi.")


@dp.message(Command("ber"), admin_only)
async def ber(m: Message, command: CommandObject):
    a = (command.args or "").split()
    to = find_user(a[0]) if a else None
    if len(a) < 3 or not to or not a[2].isdigit() or a[1].lower() not in ("pul", "olmos", "aniwu"):
        return await m.answer("Foydalanish: /ber <@user|id> pul|olmos|aniwu <son|raqam> [son]")
    kind, n = a[1].lower(), int(a[2])
    if kind == "aniwu":
        an = get_aniwu(n)
        if not an:
            return await m.answer("❌ Bunday Aniwu yo'q.")
        cnt = int(a[3]) if len(a) > 3 and a[3].isdigit() else 1
        await give(to["id"], n, cnt)
        await safe_send(to["id"], f"🎁 Admin sizga {cnt} ta Aniwu yubordi:\n{label(an)}")
    else:
        change_bal(to["id"], "money" if kind == "pul" else "diamonds", n)
        await safe_send(to["id"], f"🎁 Admin sizga {n:,} {'💰 pul' if kind == 'pul' else '💎 olmos'} yubordi!")
    await m.answer("✅ Yuborildi.")


@dp.message(Command("hammaga"), admin_only)
async def hammaga(m: Message, command: CommandObject):
    a = (command.args or "").split()
    if len(a) != 2 or not a[1].isdigit() or a[0] not in ("pul", "olmos", "aniwu"):
        return await m.answer("Foydalanish: /hammaga pul|olmos|aniwu <son|raqam>")
    kind, n = a[0], int(a[1])
    an = get_aniwu(n) if kind == "aniwu" else None
    if kind == "aniwu" and not an:
        return await m.answer("❌ Bunday Aniwu yo'q.")

    async def f(uid):
        if kind == "aniwu":
            await give(uid, n)
            await safe_send(uid, f"🎁 Admin hammaga sovg'a yubordi:\n{label(an)}")
        else:
            change_bal(uid, "money" if kind == "pul" else "diamonds", n)
            await safe_send(uid, f"🎁 Admin hammaga {n:,} {'💰 pul' if kind == 'pul' else '💎 olmos'} yubordi!")

    await m.answer(f"✅ {await broadcast(f)} kishiga yuborildi.")


@dp.message(Command("xabar"), admin_only)
async def xabar(m: Message, command: CommandObject):
    r = m.reply_to_message
    if r:
        ok = await broadcast(lambda uid: bot.copy_message(uid, r.chat.id, r.message_id))
    elif command.args:
        ok = await broadcast(lambda uid: bot.send_message(uid, command.args))
    else:
        return await m.answer("Foydalanish: /xabar <matn> yoki biror xabarga reply qilib /xabar")
    await m.answer(f"✅ {ok} kishiga yuborildi.")


@dp.message(Command("oyin"), admin_only)
async def oyin(m: Message, command: CommandObject):
    n = nums(command, 1)
    a = get_aniwu(n[0]) if n else None
    if not a:
        return await m.answer("Foydalanish: /oyin <aniwu raqami>")
    ex("INSERT OR REPLACE INTO game VALUES(1,?)", a["id"])
    cap = ("🎮 O'YIN!\nBu qaysi Aniwu? Birinchi bo'lib ISMI va DARAJASINI yozing 👇\n"
           "Masalan: Naruto epic\n🎁 Yutuq: shu Aniwu!")
    ok = await broadcast(lambda uid: bot.send_photo(uid, a["photo"], caption=cap))
    await m.answer(f"🎮 O'yin boshlandi, {ok} kishiga yuborildi.")


@dp.message(Command("stiker"), admin_only)
async def set_sticker(m: Message, command: CommandObject):
    r = m.reply_to_message
    if not (command.args and r and r.sticker):
        return await m.answer("Stikerga reply qilib yozing: /stiker sandiq (ultimate, toplam, yutuq)")
    ex("INSERT OR REPLACE INTO stickers VALUES(?,?)", command.args.strip().lower(), r.sticker.file_id)
    await m.answer("✅ Stiker saqlandi.")


# ---------- Mini App ("Ilova") serveri ----------
HERE = os.path.dirname(os.path.abspath(__file__))
PHOTOS = {}


def verify(init_data):
    """Telegram initData imzosini tekshiradi, foydalanuvchini qaytaradi."""
    try:
        d = dict(parse_qsl(init_data, strict_parsing=True))
        h = d.pop("hash")
        s = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        if hmac.compare_digest(hmac.new(key, s.encode(), hashlib.sha256).hexdigest(), h):
            return json.loads(d["user"])
    except Exception:
        pass
    return None


def api(fn):
    async def wrapper(req):
        body = await req.json()
        u = verify(body.get("initData", ""))
        if not u or not q1("SELECT 1 FROM users WHERE id=?", u["id"]):
            return web.json_response({"error": "Botda /start bosing."}, status=401)
        return web.json_response(await fn(u["id"], body))
    return wrapper


def aj(r):
    return {"id": r["id"], "name": r["name"], "rarity": r["rarity"], "price": r["price"]}


async def api_me(uid, body):
    u, own = q1("SELECT * FROM users WHERE id=?", uid), is_owner(uid)
    used = u["box_used"] if u["box_date"] == str(date.today()) else 0
    inv = ([dict(aj(r), qty=None) for r in q("SELECT * FROM aniwus ORDER BY id")] if own else
           [dict(aj(r), qty=r["qty"]) for r in q(
               "SELECT a.*, i.qty FROM inv i JOIN aniwus a ON a.id=i.aid WHERE i.uid=? ORDER BY a.id", uid)])
    cols = []
    for c in q("SELECT * FROM collections"):
        items = [dict(aj(a), own=qty(uid, a["id"]) > 0) for a in q("SELECT * FROM aniwus WHERE col_id=?", c["id"])]
        cols.append({"name": c["name"], "reward": c["reward"], "items": items,
                     "have": sum(i["own"] for i in items)})
    mk = [dict(aj(r), price=r["mprice"], base=r["price"], mid=r["mid"], mine=r["seller"] == uid) for r in q(
        "SELECT m.id mid, m.price mprice, m.seller, a.* FROM market m JOIN aniwus a ON a.id=m.aid "
        "ORDER BY m.id DESC LIMIT 40")]
    return {"role": "owner" if own else "admin" if uid in ADMINS else "member", "name": u["name"],
            "money": None if own else u["money"], "diamonds": None if own else u["diamonds"],
            "free": max(FREE_BOXES_PER_DAY - used, 0) + u["extra_boxes"], "price": BOX_PRICE,
            "inv": inv, "cols": cols, "market": mk, "stats": stats() if uid in ADMINS else None}


async def api_open(uid, body):
    a, note = open_box(uid)
    if not a:
        return {"error": note}
    await give(uid, a["id"])
    return {"aniwu": aj(a), "note": note}


async def api_buy(uid, body):
    ok, msg = await do_buy(uid, int(body.get("mid", 0)))
    return {"ok": ok, "msg": msg}


async def api_exchange(uid, body):
    kind = body.get("kind")
    try:
        amount = int(body.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0
    if amount < 1:
        return {"error": "1 yoki undan katta son kiriting."}
    if kind == "olmos":
        if not has_bal(uid, "money", amount * MONEY_PER_DIAMOND):
            return {"error": "Pul yetarli emas."}
        change_bal(uid, "money", -amount * MONEY_PER_DIAMOND)
        change_bal(uid, "diamonds", amount)
        return {"msg": f"{amount} 💎 olmos oldingiz!"}
    if kind == "olmos_pul":
        if not has_bal(uid, "diamonds", amount):
            return {"error": "Olmos yetarli emas."}
        change_bal(uid, "diamonds", -amount)
        change_bal(uid, "money", amount * MONEY_PER_DIAMOND)
        return {"msg": f"{amount * MONEY_PER_DIAMOND:,} 💰 pul oldingiz!"}
    return {"error": "Ayirboshlash turi noto‘g‘ri."}


async def photo(req):
    a = get_aniwu(int(req.match_info["aid"]))
    if not a:
        raise web.HTTPNotFound()
    if a["photo"] not in PHOTOS:
        f = await bot.get_file(a["photo"])
        PHOTOS[a["photo"]] = (await bot.download_file(f.file_path)).read()
    return web.Response(body=PHOTOS[a["photo"]], content_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})


async def index(req):
    return web.FileResponse(os.path.join(HERE, "app.html"))


async def main():
    if WEBAPP_URL:
        app = web.Application()
        app.add_routes([web.get("/", index), web.get("/photo/{aid}", photo),
                        web.post("/api/me", api(api_me)), web.post("/api/open", api(api_open)),
                        web.post("/api/buy", api(api_buy)), web.post("/api/exchange", api(api_exchange))])
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        # chatdagi ko'k "Ilova" tugmasi
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Ilova", web_app=WebAppInfo(url=WEBAPP_URL)))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
