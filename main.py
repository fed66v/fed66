import os
import sqlite3
import discord
from discord.ext import commands
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
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

cache = {}

def load_cache():
    global cache
    conn = sqlite3.connect("data.db")
    c = conn.cursor()
    c.execute("SELECT name, user_id FROM users")
    rows = c.fetchall()
    cache = {str(name).lower(): str(user_id) for name, user_id in rows}
    conn.close()
    print(f"✅ Loaded {len(cache)} records")

@bot.event
async def on_ready():
    load_cache()
    print(f"🤖 Logged in as {bot.user}")

@bot.command()
async def ids(ctx, *, names: str):
    names_list = names.replace(",", " ").split()
    found_ids = []

    for name in names_list:
        if name.lower() in cache:
            found_ids.append(cache[name.lower()])

    if not found_ids:
        await ctx.send("❌ لم يتم العثور على أي ID")
        return

    await ctx.send(
        "```" + "\n".join(found_ids) + "```"
        f"\n📌 تم العثور على: {len(found_ids)}"
    )

token = os.getenv("TOKEN")
if not token:
    raise RuntimeError("TOKEN غير موجود")

bot.run(token)
