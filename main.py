import os
import re
import sqlite3
import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask
from threading import Thread

# =========================
# Keep Alive (Render)
# =========================
app = Flask(__name__)

@app.get("/")
def home():
    return "Bot is alive!"

def run_web():
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

Thread(target=run_web, daemon=True).start()

# =========================
# Discord Bot
# =========================
intents = discord.Intents.default()
intents.message_content = True  # لازم لأوامر !

bot = commands.Bot(command_prefix="!", intents=intents)

DB_PATH = "data.db"

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
    # "c -48" -> "c-48"
    s = str(code).strip().lower().replace(" ", "")
    return s

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
    """
    جدول users:
    users(
      name TEXT PRIMARY KEY,   -- نخزنه normalized
      code TEXT UNIQUE,
      user_id TEXT NOT NULL
    )
    """
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
        # migrate old users(name,user_id) -> new with code NULL
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
        code_clean = normalize_code(code) if code is not None else None
        record = (nn, code_clean, str(user_id))

        cache_name[nn] = record
        if code_clean:
            cache_code[code_clean] = record

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
    """
    key: اسم أو كود
    يرجع (name, code, user_id) أو None
    """
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
    """
    يعدّل سجل واحد بالاسم أو الكود.
    أي قيمة None = لا تغيّر.
    """
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
# Lookup + nice output
# =========================
def split_query_items(query: str):
    """
    يدعم:
    - متعدد العناصر (مسافات/فواصل)
    - اسم كامل بجملة
    """
    q = str(query).strip()
    items = []
    if not q:
        return items

    # 1) كامل كاسم (حتى لو فيه مسافات)
    items.append(q)

    # 2) تفكيك حسب الفواصل/المسافات
    raw = q.replace(",", " ")
    for p in raw.split():
        if p.strip():
            items.append(p.strip())
    return items

def lookup_records(query: str):
    """
    يرجع قائمة Records: (name, code, user_id)
    بالبحث عن الاسم أو الكود، ويدعم أكثر من عنصر.
    """
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
            name_db, code_db, uid_db = rec
            if uid_db not in seen_ids:
                found.append(rec)
                seen_ids.add(uid_db)

    return found

def format_results(records):
    """
    يعطي:
    1) نص مرتب: code | name | id
    2) ids فقط
    """
    lines = []
    ids_only = []
    for name_db, code_db, uid_db in records:
        code_show = code_db if code_db else "-"
        name_show = name_db  # الاسم عندنا normalized (عربي ما يتأثر)
        lines.append(f"{code_show} | {name_show} | {uid_db}")
        ids_only.append(uid_db)

    pretty = "```" + "\n".join(lines) + "```" if lines else "```-```"
    ids_block = "```" + "\n".join(ids_only) + "```" if ids_only else "```-```"
    return pretty, ids_block

# =========================
# Bulk parsing (works for multiline AND single-line)
# =========================
def parse_bulk_any(text: str):
    """
    يدعم:
    - متعدد أسطر: ID  الاسم  الكود
    - سطر واحد طويل (سلاش): ID الاسم الكود ID الاسم الكود ...
    يعتمد على اكتشاف IDs (15+ رقم) كبداية لكل سجل.
    """
    raw = str(text).strip()
    if not raw:
        return []

    # إذا فيه أسطر، عالج كسطور
    if "\n" in raw:
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        entries = []
        for line in lines:
            # أسهل: أول رقم = ID، آخر كلمة = code، الوسط = الاسم
            parts = line.split()
            if len(parts) < 3:
                entries.append((None, None, None, line))
                continue
            user_id = parts[0]
            code = parts[-1]
            name = " ".join(parts[1:-1])
            entries.append((user_id, name, normalize_code(code), None))
        return entries

    # سطر واحد: قسمه عند كل ID
    # يعيد قائمة segments، كل segment يبدأ بـ ID
    segments = [s.strip() for s in re.split(r'(?=\d{15,})', raw) if s.strip()]
    entries = []
    for seg in segments:
        parts = seg.split()
        if len(parts) < 3:
            entries.append((None, None, None, seg))
            continue
        user_id = parts[0]
        code = parts[-1]
        name = " ".join(parts[1:-1])
        entries.append((user_id, name, normalize_code(code), None))
    return entries

def bulk_upsert(text: str):
    parsed = parse_bulk_any(text)
    ok, bad = 0, 0
    bad_lines = []

    for user_id, name, code, errline in parsed:
        if errline is not None:
            bad += 1
            bad_lines.append(errline)
            continue
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

# =========================
# Events
# =========================
@bot.event
async def on_ready():
    init_db()
    load_cache()
    await bot.tree.sync()
    await bot.change_presence(activity=discord.Game(name="/ids | /bulkadd | /delete | /clear | /edit"))
    print(f"🤖 Logged in as {bot.user} | Slash commands synced")

# =========================
# SLASH COMMANDS
# =========================
@bot.tree.command(name="ids", description="بحث ID بالاسم أو الكود (يدعم أكثر من عنصر)")
@app_commands.describe(query="اكتب اسم/كود أو أكثر (مثال: جاسم السلمي c-61 H-07)")
async def slash_ids(interaction: discord.Interaction, query: str):
    records = lookup_records(query)
    if not records:
        embed = discord.Embed(
            title="❌ ما لقيت نتائج",
            description="اكتب الاسم أو الكود.\nيدعم: مسافات / , / _ / - / C أو c"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    pretty, ids_block = format_results(records)

    embed = discord.Embed(title="✅ النتائج (كود | اسم | ID)")
    embed.add_field(name=f"📌 العدد: {len(records)}", value=pretty, inline=False)
    embed.add_field(name="📋 IDs فقط للنسخ", value=ids_block, inline=False)
    embed.set_footer(text=f"طلب بواسطة: {interaction.user}")
    await interaction.response.send_message(embed=embed, ephemeral=False)

@bot.tree.command(name="add", description="إضافة شخص (ID + الاسم + الكود) - للإدمن فقط")
@app_commands.describe(user_id="الآيدي", name="الاسم", code="الكود مثل c-61 أو H-07")
async def slash_add(interaction: discord.Interaction, user_id: str, name: str, code: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ هذا الأمر للإدمن فقط.", ephemeral=True)
        return

    if not is_valid_id(user_id):
        await interaction.response.send_message("❌ الآيدي غير صحيح (أرقام فقط).", ephemeral=True)
        return

    rec = upsert_user(name, code, user_id)
    n, c, uid = rec
    embed = discord.Embed(title="✅ تمّت الإضافة/التحديث")
    embed.add_field(name="الاسم", value=n, inline=False)
    embed.add_field(name="الكود", value=c, inline=False)
    embed.add_field(name="ID", value=f"`{uid}`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="bulkadd", description="إضافة/تحديث الكل (جدول كامل) - للإدمن فقط")
@app_commands.describe(data="الصق البيانات (حتى لو بسطر واحد): ID الاسم الكود ID الاسم الكود ...")
async def slash_bulkadd(interaction: discord.Interaction, data: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ هذا الأمر للإدمن فقط.", ephemeral=True)
        return

    ok, bad, bad_lines = bulk_upsert(data)
    msg = f"✅ تمت إضافة/تحديث: {ok}\n❌ أسطر/سجلات فشلت: {bad}"
    if bad_lines:
        msg += "\n\nأول أخطاء:\n```" + "\n".join(bad_lines[:5]) + "```"
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="bulkedit", description="تعديل/تحديث الكل دفعة وحدة (نفس bulkadd) - للإدمن فقط")
@app_commands.describe(data="الصق البيانات (حتى لو بسطر واحد): ID الاسم الكود ...")
async def slash_bulkedit(interaction: discord.Interaction, data: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ هذا الأمر للإدمن فقط.", ephemeral=True)
        return

    ok, bad, bad_lines = bulk_upsert(data)
    msg = f"✅ تم تعديل/تحديث: {ok}\n❌ سجلات فشلت: {bad}"
    if bad_lines:
        msg += "\n\nأول أخطاء:\n```" + "\n".join(bad_lines[:5]) + "```"
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="delete", description="حذف شخص واحد بالاسم أو الكود - للإدمن فقط")
@app_commands.describe(key="اسم أو كود (مثال: c-61 أو جاسم السلمي)")
async def slash_delete(interaction: discord.Interaction, key: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ هذا الأمر للإدمن فقط.", ephemeral=True)
        return

    ok, row = delete_one_by_key(key)
    if not ok:
        await interaction.response.send_message("❌ ما لقيت سجل بهذا الاسم/الكود.", ephemeral=True)
        return

    name_db, code_db, uid_db = row
    embed = discord.Embed(title="🗑️ تم الحذف")
    embed.add_field(name="الاسم", value=str(name_db), inline=False)
    embed.add_field(name="الكود", value=str(code_db), inline=False)
    embed.add_field(name="ID", value=f"`{uid_db}`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="clear", description="حذف الكل (يمسح جميع البيانات) - للإدمن فقط")
async def slash_clear(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ هذا الأمر للإدمن فقط.", ephemeral=True)
        return
    delete_all()
    await interaction.response.send_message("🧹 تم حذف جميع السجلات (الكل).", ephemeral=True)

@bot.tree.command(name="edit", description="تعديل شخص واحد (بالاسم أو الكود) - للإدمن فقط")
@app_commands.describe(
    key="اسم أو كود الشخص الحالي",
    name="اسم جديد (اختياري)",
    code="كود جديد (اختياري)",
    user_id="ID جديد (اختياري)"
)
async def slash_edit(interaction: discord.Interaction, key: str, name: str = None, code: str = None, user_id: str = None):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ هذا الأمر للإدمن فقط.", ephemeral=True)
        return
    try:
        ok, before, after = edit_one(key, name, code, user_id)
    except ValueError as e:
        await interaction.response.send_message(f"❌ {e}", ephemeral=True)
        return

    if not ok:
        await interaction.response.send_message("❌ ما لقيت سجل بهذا الاسم/الكود.", ephemeral=True)
        return

    b = before
    a = after
    embed = discord.Embed(title="✏️ تم التعديل")
    embed.add_field(name="قبل", value=f"{b[1]} | {b[0]} | {b[2]}", inline=False)
    embed.add_field(name="بعد", value=f"{a[1]} | {a[0]} | {a[2]}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="reload", description="تحديث الكاش من قاعدة البيانات - للإدمن فقط")
async def slash_reload(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ هذا الأمر للإدمن فقط.", ephemeral=True)
        return
    load_cache()
    await interaction.response.send_message("✅ تم تحديث الكاش.", ephemeral=True)

# =========================
# PREFIX COMMANDS (!)
# =========================
@bot.command(name="ids")
async def prefix_ids(ctx, *, query: str):
    records = lookup_records(query)
    if not records:
        await ctx.send("❌ لم يتم العثور على نتائج (ابحث بالاسم أو الكود)")
        return

    pretty, ids_block = format_results(records)

    embed = discord.Embed(title="✅ النتائج (كود | اسم | ID)")
    embed.add_field(name=f"📌 العدد: {len(records)}", value=pretty, inline=False)
    embed.add_field(name="📋 IDs فقط للنسخ", value=ids_block, inline=False)
    embed.set_footer(text=f"طلب بواسطة: {ctx.author}")
    await ctx.send(embed=embed)

@bot.command(name="add")
@commands.has_permissions(administrator=True)
async def prefix_add(ctx, user_id: str, code: str, *, name: str):
    # صيغة: !add 878... c-61 فهد الدوسري
    if not is_valid_id(user_id):
        await ctx.send("❌ صيغة خاطئة.\nمثال: `!add 878450962879098880 c-61 فهد الدوسري`")
        return
    rec = upsert_user(name, code, user_id)
    await ctx.send(f"✅ تم إضافة/تحديث: **{rec[0]}** | `{rec[1]}` | `{rec[2]}`")

@bot.command(name="bulkadd")
@commands.has_permissions(administrator=True)
async def prefix_bulkadd(ctx, *, data: str):
    ok, bad, _ = bulk_upsert(data)
    await ctx.send(f"✅ تمت إضافة/تحديث: {ok}\n❌ أسطر/سجلات فشلت: {bad}")

@bot.command(name="bulkedit")
@commands.has_permissions(administrator=True)
async def prefix_bulkedit(ctx, *, data: str):
    ok, bad, _ = bulk_upsert(data)
    await ctx.send(f"✅ تم تعديل/تحديث: {ok}\n❌ أسطر/سجلات فشلت: {bad}")

@bot.command(name="delete")
@commands.has_permissions(administrator=True)
async def prefix_delete(ctx, *, key: str):
    ok, row = delete_one_by_key(key)
    if not ok:
        await ctx.send("❌ ما لقيت سجل بهذا الاسم/الكود.")
        return
    await ctx.send(f"🗑️ تم الحذف: {row[1]} | {row[0]} | {row[2]}")

@bot.command(name="clear")
@commands.has_permissions(administrator=True)
async def prefix_clear(ctx):
    delete_all()
    await ctx.send("🧹 تم حذف جميع السجلات (الكل).")

@bot.command(name="edit")
@commands.has_permissions(administrator=True)
async def prefix_edit(ctx, key: str, field: str, *, value: str):
    """
    !edit c-61 id 123...
    !edit c-61 name فهد الدوسري
    !edit c-61 code c-70
    """
    field = field.strip().lower()
    name = code = user_id = None

    if field in ["id", "user_id", "ايدي", "آيدي"]:
        user_id = value
    elif field in ["name", "اسم"]:
        name = value
    elif field in ["code", "كود"]:
        code = value
    else:
        await ctx.send("❌ الحقل لازم يكون: name أو code أو id\nمثال: `!edit c-61 id 123...`")
        return

    try:
        ok, before, after = edit_one(key, name, code, user_id)
    except ValueError as e:
        await ctx.send(f"❌ {e}")
        return

    if not ok:
        await ctx.send("❌ ما لقيت سجل بهذا الاسم/الكود.")
        return

    await ctx.send(f"✏️ تم التعديل:\nقبل: {before}\nبعد: {after}")

# =========================
# Run
# =========================
token = os.getenv("TOKEN")
if not token:
    raise RuntimeError("❌ TOKEN غير موجود في Render Environment Variables")
bot.run(token)
