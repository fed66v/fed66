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
# Discord Bot Setup
# =========================
intents = discord.Intents.default()
intents.message_content = True  # لازم لأوامر ! (البريفكس)

bot = commands.Bot(command_prefix="!", intents=intents)

DB_PATH = "data.db"
cache = {}

def init_db():
    """يتأكد أن جدول users موجود"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            name TEXT PRIMARY KEY,
            user_id TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def normalize_name(name: str) -> str:
    """
    توحيد الاسم للبحث:
    - يحوّل '_' و '-' لمسافة
    - يحذف الزوائد
    - lowercase
    """
    return name.replace("_", " ").replace("-", " ").strip().lower()

def load_cache():
    global cache
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name, user_id FROM users")
    rows = c.fetchall()
    conn.close()
    cache = {normalize_name(n): str(uid) for n, uid in rows}
    print(f"✅ Loaded {len(cache)} records into cache")

def upsert_user(name: str, user_id: str):
    clean_name = normalize_name(name)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (name, user_id) VALUES (?, ?)", (clean_name, user_id))
    conn.commit()
    conn.close()

    cache[clean_name] = user_id
    return clean_name

def find_user_ids_by_names(names_text: str):
    # يدعم: "فهد الدوسري" أو "فهد_الدوسري" أو "فهد-الدوسري" وأيضًا قوائم مفصولة بفواصل
    raw = names_text.replace(",", " ")
    parts = raw.split()

    found = []
    for p in parts:
        key = normalize_name(p)
        if key in cache:
            found.append(cache[key])

    # إذا الشخص كتب اسم كامل فيه مسافات (مثل: فهد الدوسري) نجرّب ككتلة كاملة بعد التطبيع
    full_key = normalize_name(names_text)
    if full_key in cache and cache[full_key] not in found:
        found.append(cache[full_key])

    return found

@bot.event
async def on_ready():
    init_db()
    load_cache()

    # Sync للـ Slash Commands
    await bot.tree.sync()

    await bot.change_presence(activity=discord.Game(name="/ids أو !ids للبحث | /add أو !add للإضافة"))
    print(f"🤖 Logged in as {bot.user} | Slash commands synced")

# =========================
# SLASH: /add
# =========================
@bot.tree.command(name="add", description="إضافة اسم مع ID (للإدمن فقط)")
@app_commands.describe(
    user_id="الآيدي (مثال: 878450962879098880)",
    name="الاسم (مثال: فهد الدوسري)"
)
async def slash_add(interaction: discord.Interaction, user_id: str, name: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ هذا الأمر للإدمن فقط.", ephemeral=True)
        return

    if not user_id.isdigit() or len(user_id) < 15:
        await interaction.response.send_message("❌ الآيدي غير صحيح (لازم أرقام فقط).", ephemeral=True)
        return

    clean_name = upsert_user(name, user_id)

    embed = discord.Embed(title="✅ تمّت الإضافة", description="تم حفظ البيانات بنجاح.")
    embed.add_field(name="الاسم", value=name, inline=False)
    embed.add_field(name="الاسم (للبحث)", value=clean_name, inline=False)
    embed.add_field(name="ID", value=f"`{user_id}`", inline=False)
    embed.set_footer(text=f"بواسطة: {interaction.user}")

    await interaction.response.send_message(embed=embed, ephemeral=True)

# =========================
# SLASH: /ids
# =========================
@bot.tree.command(name="ids", description="البحث عن ID بالاسم")
@app_commands.describe(name="اكتب الاسم (مثال: فهد الدوسري)")
async def slash_ids(interaction: discord.Interaction, name: str):
    key = normalize_name(name)

    if key not in cache:
        embed = discord.Embed(
            title="❌ ما لقيت نتيجة",
            description="تأكد من الاسم وحاول مرة ثانية.\nيدعم: مسافات / _ / -"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    user_id = cache[key]
    embed = discord.Embed(title="✅ نتيجة البحث", description=f"**{name}**")
    embed.add_field(name="ID", value=f"```{user_id}```", inline=False)
    embed.set_footer(text=f"طلب بواسطة: {interaction.user}")
    await interaction.response.send_message(embed=embed, ephemeral=False)

# =========================
# PREFIX: !add 878... فهد الدوسري
# =========================
@bot.command(name="add")
@commands.has_permissions(administrator=True)
async def prefix_add(ctx, user_id: str, *, name: str):
    if not user_id.isdigit() or len(user_id) < 15:
        await ctx.send("❌ الآيدي غير صحيح (لازم أرقام فقط).")
        return

    clean_name = upsert_user(name, user_id)

    embed = discord.Embed(title="✅ تمّت الإضافة", description="تم حفظ البيانات بنجاح.")
    embed.add_field(name="الاسم", value=name, inline=False)
    embed.add_field(name="الاسم (للبحث)", value=clean_name, inline=False)
    embed.add_field(name="ID", value=f"`{user_id}`", inline=False)
    embed.set_footer(text=f"بواسطة: {ctx.author}")

    await ctx.send(embed=embed)

@prefix_add.error
async def prefix_add_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ هذا الأمر للإدمن فقط.")
    else:
        await ctx.send("❌ صيغة الأمر:\n`!add <id> <name>`\nمثال: `!add 878... فهد الدوسري`")

# =========================
# PREFIX: !ids فهد الدوسري / !ids فهد_الدوسري
# =========================
@bot.command(name="ids")
async def prefix_ids(ctx, *, names: str):
    found_ids = find_user_ids_by_names(names)

    if not found_ids:
        await ctx.send("❌ لم يتم العثور على أي ID")
        return

    embed = discord.Embed(title="✅ النتائج", description="تم العثور على التالي:")
    embed.add_field(name=f"📌 العدد: {len(found_ids)}", value="```" + "\n".join(found_ids) + "```", inline=False)
    embed.set_footer(text=f"طلب بواسطة: {ctx.author}")
    await ctx.send(embed=embed)

# =========================
# Run
# =========================
token = os.getenv("TOKEN")
if not token:
    raise RuntimeError("❌ TOKEN غير موجود في Render Environment Variables")

bot.run(token)
