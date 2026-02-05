import os
import re
import time
import asyncio
import sqlite3
from io import BytesIO
from threading import Thread

import discord
from discord.ext import commands
from discord import app_commands

from flask import Flask

# =========================
# Keep Alive (Render)
# =========================
app = Flask(__name__)

@app.get("/")
def home():
    return "Bot is alive!"

def run_web():
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

Thread(target=run_web, daemon=True).start()

# =========================
# Discord Bot
# =========================
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

DB_PATH = "data.db"

PANEL_CHANNEL_ID = int(os.getenv("PANEL_CHANNEL_ID", "0") or "0")
PANEL_MESSAGE_ID = int(os.getenv("PANEL_MESSAGE_ID", "0") or "0")

# caches:
# normalized_name -> (name, code, user_id)
cache_name = {}
# normalized_code -> (name, code, user_id)
cache_code = {}

# =========================
# Normalize helpers
# =========================
def normalize_name(name: str) -> str:
    return str(name).replace("_", " ").replace("-", " ").strip().lower()

def normalize_code(code: str) -> str:
    return str(code).strip().lower().replace(" ", "")

def is_valid_id(user_id: str) -> bool:
    user_id = str(user_id).strip()
    return user_id.isdigit() and len(user_id) >= 15

# =========================
# DB init + migration
# =========================
def table_exists(conn, name: str) -> bool:
    c = conn.cursor()
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return c.fetchone() is not None

def get_table_columns(conn, table: str):
    c = conn.cursor()
    c.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in c.fetchall()]

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    if not table_exists(conn, "users"):
        c.execute("""
            CREATE TABLE users (
                name TEXT PRIMARY KEY,
                code TEXT UNIQUE,
                user_id TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()
        return

    cols = get_table_columns(conn, "users")
    if "code" not in cols:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users_new (
                name TEXT PRIMARY KEY,
                code TEXT UNIQUE,
                user_id TEXT NOT NULL
            )
        """)
        c.execute("SELECT name, user_id FROM users")
        rows = c.fetchall()
        for n, uid in rows:
            nn = normalize_name(n)
            c.execute(
                "INSERT OR REPLACE INTO users_new (name, code, user_id) VALUES (?, NULL, ?)",
                (nn, str(uid))
            )
        c.execute("DROP TABLE users")
        c.execute("ALTER TABLE users_new RENAME TO users")
        conn.commit()

    conn.close()

# =========================
# Cache
# =========================
def load_cache():
    global cache_name, cache_code
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name, code, user_id FROM users")
    rows = c.fetchall()
    conn.close()

    cache_name = {}
    cache_code = {}

    for name, code, user_id in rows:
        nn = normalize_name(name)
        cc = normalize_code(code) if code is not None else None
        rec = (nn, cc, str(user_id))
        cache_name[nn] = rec
        if cc:
            cache_code[cc] = rec

    print(f"✅ Loaded {len(cache_name)} names and {len(cache_code)} codes into cache")

# =========================
# Core operations
# =========================
def upsert_user(name: str, code: str, user_id: str):
    nn = normalize_name(name)
    cc = normalize_code(code)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO users (name, code, user_id) VALUES (?, ?, ?)",
        (nn, cc, str(user_id))
    )
    conn.commit()
    conn.close()

    rec = (nn, cc, str(user_id))
    cache_name[nn] = rec
    cache_code[cc] = rec
    return rec

def find_row_by_key(key: str):
    nn = normalize_name(key)
    cc = normalize_code(key)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("SELECT name, code, user_id FROM users WHERE name = ?", (nn,))
    row = c.fetchone()
    if row is None:
        c.execute("SELECT name, code, user_id FROM users WHERE code = ?", (cc,))
        row = c.fetchone()

    conn.close()
    if not row:
        return None

    name_db, code_db, uid_db = row
    return (normalize_name(name_db), normalize_code(code_db) if code_db else None, str(uid_db))

def delete_one_by_key(key: str):
    row = find_row_by_key(key)
    if not row:
        return False, None

    name_db, code_db, uid_db = row
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE name = ?", (name_db,))
    conn.commit()
    conn.close()

    cache_name.pop(name_db, None)
    if code_db:
        cache_code.pop(code_db, None)

    return True, (name_db, code_db, uid_db)

def delete_all():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM users")
    conn.commit()
    conn.close()
    load_cache()

def edit_one(key: str, new_name=None, new_code=None, new_user_id=None):
    row = find_row_by_key(key)
    if not row:
        return False, None, None

    old_name, old_code, old_uid = row

    final_name = normalize_name(new_name) if new_name and str(new_name).strip() else old_name
    final_code = normalize_code(new_code) if new_code and str(new_code).strip() else old_code
    final_uid = str(new_user_id).strip() if new_user_id and str(new_user_id).strip() else old_uid

    if not is_valid_id(final_uid):
        raise ValueError("ID لازم يكون أرقام فقط وصحيح")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO users (name, code, user_id) VALUES (?, ?, ?)",
        (final_name, final_code, final_uid)
    )
    if old_name != final_name:
        c.execute("DELETE FROM users WHERE name = ?", (old_name,))
    conn.commit()
    conn.close()

    load_cache()
    before = (old_name, old_code, old_uid)
    after = (final_name, final_code, final_uid)
    return True, before, after

# =========================
# Lookup + formatting
# =========================
def split_query_items(query: str):
    q = str(query).strip()
    items = []
    if not q:
        return items

    items.append(q)  # full phrase
    raw = q.replace(",", " ")
    for p in raw.split():
        if p.strip():
            items.append(p.strip())
    return items

def lookup_records(query: str):
    found = []
    seen_ids = set()

    for item in split_query_items(query):
        nn = normalize_name(item)
        cc = normalize_code(item)

        rec = None
        if nn in cache_name:
            rec = cache_name[nn]
        elif cc in cache_code:
            rec = cache_code[cc]

        if rec:
            n, c, uid = rec
            if uid not in seen_ids:
                found.append(rec)
                seen_ids.add(uid)

    return found

def format_results(records):
    lines = []
    ids_only = []
    for n, c, uid in records:
        lines.append(f"{c or '-'} | {n} | {uid}")
        ids_only.append(uid)

    pretty = "```" + "\n".join(lines) + "```" if lines else "```-```"
    ids_block = "```" + "\n".join(ids_only) + "```" if ids_only else "```-```"
    return pretty, ids_block

def list_all_records():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name, code, user_id FROM users ORDER BY code IS NULL, code, name")
    rows = c.fetchall()
    conn.close()

    out = []
    for n, code, uid in rows:
        out.append((normalize_name(n), normalize_code(code) if code else None, str(uid)))
    return out

# =========================
# Bulk parsing (multiline OR single-line)
# =========================
def parse_bulk_any(text: str):
    raw = str(text).strip()
    if not raw:
        return []

    # multiline
    if "\n" in raw:
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        entries = []
        for line in lines:
            parts = line.split()
            if len(parts) < 3:
                continue  # تجاهل الغلط
            user_id = parts[0]
            code = parts[-1]
            name = " ".join(parts[1:-1])
            entries.append((user_id, name, normalize_code(code)))
        return entries

    # single-line: split at each ID (15+ digits)
    segments = [s.strip() for s in re.split(r'(?=\d{15,})', raw) if s.strip()]
    entries = []
    for seg in segments:
        parts = seg.split()
        if len(parts) < 3:
            continue
        user_id = parts[0]
        code = parts[-1]
        name = " ".join(parts[1:-1])
        entries.append((user_id, name, normalize_code(code)))
    return entries

def bulk_upsert(text: str):
    parsed = parse_bulk_any(text)
    ok, bad = 0, 0
    bad_lines = []

    for user_id, name, code in parsed:
        if not is_valid_id(user_id):
            bad += 1
            bad_lines.append(f"(ID غير صحيح) {user_id} | {name} | {code}")
            continue
        if not name or not code:
            bad += 1
            bad_lines.append(f"(نقص بيانات) {user_id} | {name} | {code}")
            continue

        upsert_user(name, code, user_id)
        ok += 1

    return ok, bad, bad_lines

def delete_many(keys_text: str):
    lines = [l.strip() for l in str(keys_text).splitlines() if l.strip()]
    ok, bad = 0, 0
    for key in lines:
        deleted, _ = delete_one_by_key(key)
        if deleted:
            ok += 1
        else:
            bad += 1
    return ok, bad

# =========================
# Panel UI (Buttons + Modals)
# =========================
class AddModal(discord.ui.Modal, title="➕ إضافة (ID الاسم الكود)"):
    data = discord.ui.TextInput(
        label="الصق البيانات (سطر لكل شخص) أو سطر واحد طويل",
        style=discord.TextStyle.long,
        required=True,
        max_length=4000
    )

    async def on_submit(self, interaction: discord.Interaction):
        ok, bad, bad_lines = bulk_upsert(str(self.data))
        msg = f"✅ تمت إضافة/تحديث: {ok}\n❌ سجلات فشلت: {bad}"
        if bad_lines:
            msg += "\n\nأول أخطاء:\n```" + "\n".join(bad_lines[:5]) + "```"
        await interaction.response.send_message(msg, ephemeral=True)


class DeleteModal(discord.ui.Modal, title="🗑️ حذف (اسم أو كود)"):
    data = discord.ui.TextInput(
        label="الصق الأسماء/الأكواد (سطر لكل واحد)",
        style=discord.TextStyle.long,
        required=True,
        max_length=4000
    )

    async def on_submit(self, interaction: discord.Interaction):
        ok, bad = delete_many(str(self.data))
        load_cache()
        await interaction.response.send_message(f"🗑️ تم حذف: {ok}\n❌ لم يُعثر على: {bad}", ephemeral=True)


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    def _check_channel(self, interaction: discord.Interaction) -> bool:
        return (not PANEL_CHANNEL_ID) or (interaction.channel_id == PANEL_CHANNEL_ID)

    @discord.ui.button(label="➕ إضافة (مجموعة/اسم)", style=discord.ButtonStyle.success, custom_id="panel:add")
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_channel(interaction):
            await interaction.response.send_message("❌ استخدم اللوحة في الروم المخصص فقط.", ephemeral=True)
            return
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ هذا الخيار للإدمن فقط.", ephemeral=True)
            return
        await interaction.response.send_modal(AddModal())

    @discord.ui.button(label="🗑️ حذف (مجموعة/اسم)", style=discord.ButtonStyle.danger, custom_id="panel:delete")
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_channel(interaction):
            await interaction.response.send_message("❌ استخدم اللوحة في الروم المخصص فقط.", ephemeral=True)
            return
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ هذا الخيار للإدمن فقط.", ephemeral=True)
            return
        await interaction.response.send_modal(DeleteModal())

    @discord.ui.button(label="📋 عرض الأسماء", style=discord.ButtonStyle.primary, custom_id="panel:list")
    async def list_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_channel(interaction):
            await interaction.response.send_message("❌ استخدم اللوحة في الروم المخصص فقط.", ephemeral=True)
            return

        records = list_all_records()
        if not records:
            await interaction.response.send_message("لا يوجد بيانات حاليًا.", ephemeral=True)
            return

        # عرض أول 40 فقط داخل Embed
        lines = []
        ids_only = []
        for n, c, uid in records[:40]:
            lines.append(f"{c or '-'} | {n} | {uid}")
            ids_only.append(uid)

        embed = discord.Embed(title="📋 قائمة البيانات (أول 40)")
        embed.add_field(name="كود | اسم | ID", value="```" + "\n".join(lines) + "```", inline=False)
        embed.add_field(name="IDs فقط للنسخ", value="```" + "\n".join(ids_only) + "```", inline=False)
        embed.set_footer(text=f"الإجمالي: {len(records)} | للتصدير الكامل اضغط 📤 تصدير TXT")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="📤 تصدير TXT", style=discord.ButtonStyle.secondary, custom_id="panel:export")
    async def export_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_channel(interaction):
            await interaction.response.send_message("❌ استخدم اللوحة في الروم المخصص فقط.", ephemeral=True)
            return

        records = list_all_records()
        if not records:
            await interaction.response.send_message("لا توجد بيانات للتصدير.", ephemeral=True)
            return

        # TSV format (يفتح ممتاز في Excel/Sheets)
        lines = ["code\tname\tid"]
        for n, c, uid in records:
            lines.append(f"{c or ''}\t{n}\t{uid}")
        content = "\n".join(lines)

        file = discord.File(fp=BytesIO(content.encode("utf-8")), filename="ids.txt")
        await interaction.response.send_message("📄 تم إنشاء ملف التصدير:", file=file, ephemeral=True)

# =========================
# Events
# =========================
@bot.event
async def on_ready():
    init_db()
    load_cache()
    bot.add_view(PanelView())  # keep buttons alive after restart
    await bot.tree.sync()
    await bot.change_presence(activity=discord.Game(name="لوحة IDs | /panel"))
    print(f"🤖 Logged in as {bot.user}")

# =========================
# SLASH COMMANDS
# =========================
@bot.tree.command(name="panel", description="إنشاء/تحديث لوحة التحكم في الروم المحدد (إدمن فقط)")
async def panel_cmd(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ هذا الأمر للإدمن فقط.", ephemeral=True)
        return

    if not PANEL_CHANNEL_ID:
        await interaction.response.send_message("❌ PANEL_CHANNEL_ID غير مضبوط في Render.", ephemeral=True)
        return

    channel = interaction.guild.get_channel(PANEL_CHANNEL_ID)
    if not channel:
        await interaction.response.send_message("❌ ما قدرت أوصل للروم. تأكد من Channel ID وصلاحيات البوت.", ephemeral=True)
        return

    view = PanelView()
    content = (
        "📌 **لوحة إدارة الـ IDs**\n"
        "➕ إضافة (اسم/مجموعة)\n"
        "🗑️ حذف (اسم/مجموعة)\n"
        "📋 عرض القائمة\n"
        "📤 تصدير TXT\n"
        "\nملاحظة: الإضافة/الحذف للإدمن فقط."
    )

    if PANEL_MESSAGE_ID:
        try:
            msg = await channel.fetch_message(PANEL_MESSAGE_ID)
            await msg.edit(content=content, view=view)
            await interaction.response.send_message("✅ تم تحديث اللوحة.", ephemeral=True)
            return
        except Exception:
            pass

    msg = await channel.send(content, view=view)
    await interaction.response.send_message(
        f"✅ تم إنشاء اللوحة.\nانسخ Message ID هذا وضعه في Render كـ PANEL_MESSAGE_ID:\n`{msg.id}`",
        ephemeral=True
    )

@bot.tree.command(name="ids", description="بحث ID بالاسم أو الكود (يدعم أكثر من عنصر)")
@app_commands.describe(query="مثال: فهد الدوسري c-61 H-07")
async def slash_ids(interaction: discord.Interaction, query: str):
    records = lookup_records(query)
    if not records:
        await interaction.response.send_message("❌ ما لقيت نتائج.", ephemeral=True)
        return

    pretty, ids_block = format_results(records)
    embed = discord.Embed(title="✅ النتائج (كود | اسم | ID)")
    embed.add_field(name=f"📌 العدد: {len(records)}", value=pretty, inline=False)
    embed.add_field(name="📋 IDs فقط للنسخ", value=ids_block, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=False)

@bot.tree.command(name="bulkadd", description="إضافة/تحديث جماعي - للإدمن فقط")
@app_commands.describe(data="الصق البيانات (حتى لو بسطر واحد): ID الاسم الكود ...")
async def slash_bulkadd(interaction: discord.Interaction, data: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ هذا الأمر للإدمن فقط.", ephemeral=True)
        return
    ok, bad, bad_lines = bulk_upsert(data)
    msg = f"✅ تمت إضافة/تحديث: {ok}\n❌ سجلات فشلت: {bad}"
    if bad_lines:
        msg += "\n\nأول أخطاء:\n```" + "\n".join(bad_lines[:5]) + "```"
    await interaction.response.send_message(msg, ephemeral=True)

# =========================
# PREFIX COMMANDS (!)
# =========================
@bot.command(name="ids")
async def prefix_ids(ctx, *, query: str):
    records = lookup_records(query)
    if not records:
        await ctx.send("❌ ما لقيت نتائج.")
        return
    pretty, ids_block = format_results(records)
    embed = discord.Embed(title="✅ النتائج (كود | اسم | ID)")
    embed.add_field(name=f"📌 العدد: {len(records)}", value=pretty, inline=False)
    embed.add_field(name="📋 IDs فقط للنسخ", value=ids_block, inline=False)
    await ctx.send(embed=embed)

@bot.command(name="bulkadd")
@commands.has_permissions(administrator=True)
async def prefix_bulkadd(ctx, *, data: str):
    ok, bad, _ = bulk_upsert(data)
    await ctx.send(f"✅ تمت إضافة/تحديث: {ok}\n❌ سجلات فشلت: {bad}")

# =========================
# Run (Anti 429 loop)
# =========================
token = os.getenv("TOKEN")
if not token:
    raise RuntimeError("❌ TOKEN غير موجود في Render Environment Variables")

async def main():
    async with bot:
        await bot.start(token, reconnect=True)

while True:
    try:
        asyncio.run(main())
    except discord.errors.HTTPException as e:
        msg = str(e).lower()
        if "429" in msg or "rate limited" in msg or "cloudflare" in msg:
            print("⚠️ Discord/Cloudflare rate limited (429/1015). Sleeping 15 minutes to avoid restart loop...")
            time.sleep(15 * 60)
            continue
        raise
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        time.sleep(30)
        continue
