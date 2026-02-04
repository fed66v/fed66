import os
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

cache_name = {}  # normalized name -> user_id
cache_code = {}  # normalized code -> user_id

# =========================
# Normalize helpers
# =========================
def normalize_name(name: str) -> str:
    return str(name).replace("_", " ").replace("-", " ").strip().lower()

def normalize_code(code: str) -> str:
    # "c -48" -> "c-48"
    s = str(code).strip().lower().replace(" ", "")
    s = s.replace("c-", "c-")  # keep
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
    الجدول النهائي:
    users(
      name TEXT PRIMARY KEY,
      code TEXT UNIQUE,
      user_id TEXT NOT NULL
    )

    لو عندك جدول قديم users(name,user_id) يترحل تلقائيًا (code=NULL).
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
        # migrate
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
        cache_name[nn] = str(user_id)
        if code is not None and str(code).strip():
            cc = normalize_code(code)
            cache_code[cc] = str(user_id)

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

    cache_name[nn] = str(user_id)
    cache_code[cc] = str(user_id)
    return nn, cc, str(user_id)

def lookup_ids(query: str):
    """
    يبحث بالاسم أو بالكود.
    يدعم أكثر من عنصر (مسافات/فواصل).
    يدعم الاسم الكامل (جملة).
    """
    found = []

    # 1) اسم كامل
    full_name = normalize_name(query)
    if full_name in cache_name:
        found.append(cache_name[full_name])

    # 2) عناصر منفصلة
    raw = str(query).replace(",", " ")
    parts = raw.split()

    for p in parts:
        nn = normalize_name(p)
        cc = normalize_code(p)

        if nn in cache_name:
            uid = cache_name[nn]
            if uid not in found:
                found.append(uid)
        elif cc in cache_code:
            uid = cache_code[cc]
            if uid not in found:
                found.append(uid)

    return found

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
    return row

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

    cache_name.pop(normalize_name(name_db), None)
    if code_db:
        cache_code.pop(normalize_code(code_db), None)

    return True, (name_db, code_db, uid_db)

def delete_all():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM users")
    conn.commit()
    conn.close()
    load_cache()

def edit_one(key: str, new_name: str = None, new_code: str = None, new_user_id: str = None):
    """
    يعدّل سجل واحد بالاسم أو الكود.
    أي قيمة None = لا تغيّر.
    """
    row = find_row_by_key(key)
    if not row:
        return False, None, None

    old_name, old_code, old_uid = row

    final_name = normalize_name(new_name) if new_name and str(new_name).strip() else normalize_name(old_name)
    final_code = normalize_code(new_code) if new_code and str(new_code).strip() else (normalize_code(old_code) if old_code else None)
    final_uid = str(new_user_id).strip() if new_user_id and str(new_user_id).strip() else str(old_uid)

    if not is_valid_id(final_uid):
        raise ValueError("ID لازم يكون أرقام فقط وصحيح")

    # upsert للبيانات الجديدة
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO users (name, code, user_id) VALUES (?, ?, ?)",
        (final_name, final_code, final_uid)
    )
    # إذا الاسم تغير، احذف القديم
    if normalize_name(old_name) != final_name:
        c.execute("DELETE FROM users WHERE name = ?", (old_name,))
    conn.commit()
    conn.close()

    load_cache()
    before = (old_name, old_code, old_uid)
    after = (final_name, final_code, final_uid)
    return True, before, after

# =========================
# Bulk parsing (ID  NAME  CODE)
# =========================
def parse_bulk_lines(text: str):
    """
    يقبل:
    ID<TAB>NAME<TAB>CODE
    أو ID  NAME  CODE
    أو ID NAME CODE
    """
    lines = [l.strip() for l in str(text).splitlines() if l.strip()]
    parsed = []
    for line in lines:
        line2 = line.replace("\t", "  ").strip()
        chunks = [x.strip() for x in line2.split("  ") if x.strip()]

        if len(chunks) >= 3:
            user_id, name, code = chunks[0], chunks[1], chunks[2]
        else:
            parts = line.split()
            if len(parts) < 3:
                parsed.append((None, None, None, line))
                continue
            user_id = parts[0]
            code = parts[-1]
            name = " ".join(parts[1:-1])

        code = normalize_code(code)
        parsed.append((user_id, name, code, None))
    return parsed

def bulk_upsert(text: str):
    parsed = parse_bulk_lines(text)
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
    await bot.change_presence(activity=discord.Game(name="/ids أو !ids | /bulkadd | /delete | /clear | /edit | /bulkedit"))
    print(f"🤖 Logged in as {bot.user} | Slash commands synced")

# =========================
# SLASH COMMANDS
# =========================

@bot.tree.command(name="ids", description="بحث ID بالاسم أو الكود (يدعم أكثر من عنصر)")
@app_commands.describe(query="اكتب اسم/كود أو أكثر (مثال: جاسم السلمي c-61)")
async def slash_ids(interaction: discord.Interaction, query: str):
    found = lookup_ids(query)
    if not found:
        embed = discord.Embed(title="❌ ما لقيت نتائج", description="اكتب الاسم أو الكود.\nيدعم: مسافات / , / _ / - / c أو C")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    embed = discord.Embed(title="✅ النتائج")
    embed.add_field(name=f"📌 العدد: {len(found)}", value="```" + "\n".join(found) + "```", inline=False)
    embed.set_footer(text=f"طلب بواسطة: {interaction.user}")
    await interaction.response.send_message(embed=embed, ephemeral=False)

@bot.tree.command(name="add", description="إضافة شخص (ID + الاسم + الكود) - للإدمن فقط")
@app_commands.describe(user_id="الآيدي", name="الاسم", code="الكود مثل c-61")
async def slash_add(interaction: discord.Interaction, user_id: str, name: str, code: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ هذا الأمر للإدمن فقط.", ephemeral=True)
        return

    if not is_valid_id(user_id):
        await interaction.response.send_message("❌ الآيدي غير صحيح (أرقام فقط).", ephemeral=True)
        return

    nn, cc, uid = upsert_user(name, code, user_id)
    embed = discord.Embed(title="✅ تمّت الإضافة/التحديث")
    embed.add_field(name="الاسم", value=name, inline=False)
    embed.add_field(name="الكود", value=cc, inline=False)
    embed.add_field(name="ID", value=f"`{uid}`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="bulkadd", description="إضافة/تحديث الكل (جدول كامل) - للإدمن فقط")
@app_commands.describe(data="الصق السطور: ID<TAB>الاسم<TAB>الكود (سطر لكل شخص)")
async def slash_bulkadd(interaction: discord.Interaction, data: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ هذا الأمر للإدمن فقط.", ephemeral=True)
        return

    ok, bad, bad_lines = bulk_upsert(data)
    msg = f"✅ تم إضافة/تحديث: {ok}\n❌ أسطر فشلت: {bad}"
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
    embed.add_field(name="قبل", value=f"{b[0]} | `{b[1]}` | `{b[2]}`", inline=False)
    embed.add_field(name="بعد", value=f"{a[0]} | `{a[1]}` | `{a[2]}`", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="bulkedit", description="تعديل/تحديث الكل دفعة وحدة (نفس جدول ID الاسم الكود) - للإدمن فقط")
@app_commands.describe(data="الصق السطور: ID<TAB>الاسم<TAB>الكود (سطر لكل شخص)")
async def slash_bulkedit(interaction: discord.Interaction, data: str):
    # bulkedit نفس bulkadd (تحديث جماعي)
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ هذا الأمر للإدمن فقط.", ephemeral=True)
        return

    ok, bad, bad_lines = bulk_upsert(data)
    msg = f"✅ تم تعديل/تحديث: {ok}\n❌ أسطر فشلت: {bad}"
    if bad_lines:
        msg += "\n\nأول أخطاء:\n```" + "\n".join(bad_lines[:5]) + "```"
    await interaction.response.send_message(msg, ephemeral=True)

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
    found = lookup_ids(query)
    if not found:
        await ctx.send("❌ لم يتم العثور على أي ID (ابحث بالاسم أو الكود)")
        return
    await ctx.send("```" + "\n".join(found) + "```" + f"\n📌 تم العثور على: {len(found)}")

@bot.command(name="add")
@commands.has_permissions(administrator=True)
async def prefix_add(ctx, user_id: str, code: str, *, name: str):
    # صيغة: !add 878... c-61 فهد الدوسري
    if not is_valid_id(user_id):
        await ctx.send("❌ صيغة خاطئة.\nمثال: `!add 878450962879098880 c-61 فهد الدوسري`")
        return
    nn, cc, uid = upsert_user(name, code, user_id)
    await ctx.send(f"✅ تم إضافة/تحديث: **{name}** | `{cc}` | `{uid}`")

@bot.command(name="bulkadd")
@commands.has_permissions(administrator=True)
async def prefix_bulkadd(ctx, *, data: str):
    ok, bad, _ = bulk_upsert(data)
    await ctx.send(f"✅ تم إضافة/تحديث: {ok}\n❌ أسطر فشلت: {bad}")

@bot.command(name="bulkedit")
@commands.has_permissions(administrator=True)
async def prefix_bulkedit(ctx, *, data: str):
    ok, bad, _ = bulk_upsert(data)
    await ctx.send(f"✅ تم تعديل/تحديث: {ok}\n❌ أسطر فشلت: {bad}")

@bot.command(name="delete")
@commands.has_permissions(administrator=True)
async def prefix_delete(ctx, *, key: str):
    ok, row = delete_one_by_key(key)
    if not ok:
        await ctx.send("❌ ما لقيت سجل بهذا الاسم/الكود.")
        return
    await ctx.send(f"🗑️ تم الحذف: {row[0]} | `{row[1]}` | `{row[2]}`")

@bot.command(name="clear")
@commands.has_permissions(administrator=True)
async def prefix_clear(ctx):
    delete_all()
    await ctx.send("🧹 تم حذف جميع السجلات (الكل).")

@bot.command(name="edit")
@commands.has_permissions(administrator=True)
async def prefix_edit(ctx, key: str, field: str, *, value: str):
    """
    تعديل شخص واحد:
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
