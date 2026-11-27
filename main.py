import os
import time
import asyncio
import apsw
import psycopg2
import secrets
import hashlib
import requests
import httpx
import json
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status, Query
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional
from vfs import HTTPRangeVFS

app = FastAPI()
security = HTTPBasic()

# Environment Variables
DB_URL = os.environ.get("DB_URL", "").strip()
HF_TOKEN = os.environ.get("HF_TOKEN", "").strip() or None
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

db_conn = None 

# Default Credits
DEFAULT_CREDITS = {"developer": "Cyber_insight_309", "name": "Devid", "telegram": "@cyber_insight_309"}

def hash_pass(pwd):
    return hashlib.sha256(pwd.encode()).hexdigest()

def get_admin_db():
    return psycopg2.connect(DATABASE_URL)

# ==========================================
# 🗄️ ADMIN DATABASE SETUP (Neon PostgreSQL)
# ==========================================
def init_db():
    if not DATABASE_URL: return
    try:
        conn = get_admin_db(); c = conn.cursor()
        
        c.execute('''CREATE TABLE IF NOT EXISTS admins (username TEXT PRIMARY KEY, password TEXT)''')
        c.execute("SELECT count(*) FROM admins")
        if c.fetchone()[0] == 0:
            c.execute("INSERT INTO admins (username, password) VALUES (%s, %s)", ("admin", hash_pass("CyberAdmin123")))
        
        c.execute('''CREATE TABLE IF NOT EXISTS api_keys (
            api_key TEXT PRIMARY KEY, owner_name TEXT, expiry_date TEXT, max_limit INTEGER, 
            used_count INTEGER DEFAULT 0, is_active INTEGER DEFAULT 1, created_date TEXT, 
            rate_limit_seconds INTEGER DEFAULT 1, last_request_time TEXT, allowed_ip TEXT,
            key_type TEXT DEFAULT 'id', expiry_alert_sent INTEGER DEFAULT 0
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS bots (
            bot_id SERIAL PRIMARY KEY, bot_name TEXT, bot_token TEXT UNIQUE, is_active INTEGER DEFAULT 1, created_at TEXT
        )''')
        
        c.execute("DROP TABLE IF EXISTS bot_users") 
        c.execute('''CREATE TABLE IF NOT EXISTS bot_users (
            telegram_id TEXT, bot_token TEXT, api_key TEXT, username TEXT, is_blocked INTEGER DEFAULT 0, joined_date TEXT,
            PRIMARY KEY (telegram_id, bot_token)
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS search_logs (id SERIAL PRIMARY KEY, api_key TEXT, number TEXT, status TEXT, timestamp TEXT, ip_address TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
        
        default_credits_json = json.dumps(DEFAULT_CREDITS)
        for k, v in [("maintenance", "0"), ("global_rate_limit", "1"), ("admin_chat_id", ""), 
                     ("api_credits", default_credits_json), ("bot_credits", default_credits_json)]:
            c.execute(f"INSERT INTO settings (key, value) VALUES ('{k}', '{v}') ON CONFLICT (key) DO NOTHING")
        
        conn.commit(); c.close(); conn.close()
        print("✅ Admin DB Initialized Successfully!")
    except Exception as e:
        print(f"❌ DB Init Error: {e}")

init_db()

def get_setting(key):
    conn = get_admin_db(); c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = %s", (key,))
    row = c.fetchone()
    c.close(); conn.close()
    return row[0] if row else None

def set_setting(key, value):
    conn = get_admin_db(); c = conn.cursor()
    c.execute("UPDATE settings SET value = %s WHERE key = %s", (value, key))
    conn.commit(); c.close(); conn.close()

def get_credits(credit_type="api"):
    val = get_setting(f"{credit_type}_credits")
    if val:
        try: return json.loads(val)
        except: pass
    return DEFAULT_CREDITS

# ==========================================
# 🤖 TELEGRAM SEND FUNCTIONS
# ==========================================
async def send_tg_msg_async(bot_token, chat_id, text):
    if not bot_token or not chat_id: return
    bot_token = str(bot_token).strip()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage", 
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, 
                timeout=60.0 
            )
            print(f"✅ TG SEND [{chat_id}]: {r.status_code}")
    except Exception as e:
        print(f"❌ TG SEND ERROR: {e}")

def send_tg_msg_sync(bot_token, chat_id, text):
    if not bot_token or not chat_id: return
    bot_token = str(bot_token).strip()
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage", 
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, 
            timeout=60
        )
        print(f"✅ TG SEND [{chat_id}]: {r.status_code}")
    except Exception as e:
        print(f"❌ TG SEND ERROR: {e}")

# ==========================================
# 🕒 BACKGROUND TASK (Expiry Alerts)
# ==========================================
async def check_expiry_alerts():
    while True:
        await asyncio.sleep(3600)
        try:
            def task():
                conn = get_admin_db(); c = conn.cursor()
                target_date = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
                c.execute("SELECT api_key, owner_name, expiry_date FROM api_keys WHERE expiry_date = %s AND expiry_alert_sent = 0", (target_date,))
                rows = c.fetchall()
                admin_chat = get_setting("admin_chat_id")
                for row in rows:
                    key, owner, exp = row
                    c.execute("SELECT bot_token FROM bots WHERE is_active = 1 LIMIT 1")
                    admin_bot = c.fetchone()
                    if admin_chat and admin_bot:
                        send_tg_msg_sync(admin_bot[0], admin_chat, f"⚠️ <b>Key Expiring Soon!</b>\n\nClient: {owner}\nKey: <code>{key}</code>\nExpiry: {exp}")
                    c.execute("SELECT telegram_id, bot_token FROM bot_users WHERE api_key = %s", (key,))
                    bot_users = c.fetchall()
                    for user in bot_users:
                        send_tg_msg_sync(user[1], user[0], f"⚠️ <b>Your API Key is expiring in 3 days!</b>\n\nKey: <code>{key}</code>\nExpiry: {exp}\n\nPlease contact admin to renew.")
                    c.execute("UPDATE api_keys SET expiry_alert_sent = 1 WHERE api_key = %s", (key,))
                    conn.commit()
                c.close(); conn.close()
            await asyncio.to_thread(task)
        except Exception as e: print(f"Expiry Task Error: {e}")

# ==========================================
# 🚀 LIFESPAN (Remote DB Connection)
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_conn
    print("⏳ Remote Database (50GB) se connect ho raha hai...")
    try:
        vfs = HTTPRangeVFS("httpvfs", url=DB_URL, token=HF_TOKEN)
        db_conn = apsw.Connection("remote_db.sqlite", flags=apsw.SQLITE_OPEN_READONLY, vfs="httpvfs")
        
        # 50GB DB Optimizations
        db_conn.execute("PRAGMA query_only = ON;")
        db_conn.execute("PRAGMA cache_size = -20000;") 
        db_conn.execute("PRAGMA temp_store = MEMORY;")
        
        print("✅ Remote DB ready!")
    except Exception as e:
        print(f"❌ Remote DB Error: {e}")
        raise e
    
    alert_task = asyncio.create_task(check_expiry_alerts())
    yield
    alert_task.cancel()
    if db_conn: db_conn.close()

app.router.lifespan_context = lifespan

# ==========================================
# 🤖 MULTI-BOT WEBHOOK
# ==========================================
@app.post("/webhook/{bot_token}")
async def telegram_webhook(bot_token: str, request: Request):
    bot_token = bot_token.strip()
    
    conn = get_admin_db(); c = conn.cursor()
    c.execute("SELECT is_active FROM bots WHERE bot_token = %s", (bot_token,))
    bot_row = c.fetchone()
    if not bot_row or bot_row[0] == 0:
        c.close(); conn.close()
        return {"status": "bot disabled"}
    
    try:
        data = await request.json()
        if "message" not in data or "text" not in data["message"]: return {"ok": True}
        
        chat_id = str(data["message"]["chat"]["id"])
        text = data["message"]["text"].strip()
        tg_username = data["message"]["from"].get("username", "Unknown")
        
        c.execute("SELECT is_blocked, api_key FROM bot_users WHERE telegram_id = %s AND bot_token = %s", (chat_id, bot_token))
        user_row = c.fetchone()
        
        if user_row and user_row[0] == 1:
            await send_tg_msg_async(bot_token, chat_id, "🚫 You are blocked by Admin.")
            c.close(); conn.close()
            return {"ok": True}

        if text.startswith("/start"):
            if user_row and user_row[1]:
                await send_tg_msg_async(bot_token, chat_id, f"Welcome back! You are logged in with Key: <code>{user_row[1]}</code>.\n\nSend me a <b>Mobile Number</b> or <b>ID</b> to search.")
            else:
                await send_tg_msg_async(bot_token, chat_id, "Welcome! 🔐 Please send your <b>API Key</b> to login (e.g. HTK-XXXX).")
            c.close(); conn.close()
            return {"ok": True}

        # LOGIN FLOW
        if not user_row or not user_row[1]:
            c.execute("SELECT api_key, expiry_date, is_active, key_type FROM api_keys WHERE api_key = %s", (text,))
            api_row = c.fetchone()
            if api_row:
                if api_row[2] == 0 or datetime.strptime(api_row[1], "%Y-%m-%d").date() < datetime.now().date():
                    await send_tg_msg_async(bot_token, chat_id, "❌ This API Key is expired or blocked.")
                else:
                    c.execute("""INSERT INTO bot_users (telegram_id, bot_token, api_key, username, joined_date) 
                                 VALUES (%s, %s, %s, %s, %s) 
                                 ON CONFLICT (telegram_id, bot_token) DO UPDATE SET api_key = EXCLUDED.api_key, is_blocked = 0""", 
                              (chat_id, bot_token, text, tg_username, datetime.now().strftime("%Y-%m-%d")))
                    conn.commit()
                    await send_tg_msg_async(bot_token, chat_id, f"✅ Login Successful!\nKey Type: <b>{api_row[3].upper()}</b>.\n\nNow send me a search query.")
            else:
                await send_tg_msg_async(bot_token, chat_id, "❌ Invalid API Key. Please check and send again.")
            c.close(); conn.close()
            return {"ok": True}

        # SEARCH FLOW
        api_key = user_row[1]
        c.execute("SELECT expiry_date, max_limit, used_count, is_active, rate_limit_seconds, last_request_time, key_type FROM api_keys WHERE api_key = %s", (api_key,))
        api_data = c.fetchone()
        
        if not api_data or api_data[3] == 0 or datetime.strptime(api_data[0], "%Y-%m-%d").date() < datetime.now().date():
            await send_tg_msg_async(bot_token, chat_id, "❌ Your API Key is expired or blocked.")
            c.close(); conn.close()
            return {"ok": True}

        expiry, max_limit, used_count, is_active, rate_limit, last_req_str, key_type = api_data

        if used_count >= max_limit:
            await send_tg_msg_async(bot_token, chat_id, "❌ API Limit Exceeded. Please contact admin.")
            c.close(); conn.close()
            return {"ok": True}
            
        if last_req_str:
            last_req_time = datetime.strptime(last_req_str, "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - last_req_time).total_seconds() < rate_limit:
                await send_tg_msg_async(bot_token, chat_id, f"⏳ Rate limit exceeded. Please wait {rate_limit} seconds.")
                c.close(); conn.close()
                return {"ok": True}

        search_field = "mobile"
        if key_type == "universal":
            if text.isdigit(): search_field = "mobile"
            else: search_field = "id"
        elif key_type == "id": search_field = "id"
        elif key_type == "mobile": search_field = "mobile"

        cursor = db_conn.cursor()
        # Naye Columns: id, mobile, name, fname, address, alt, circle, email
        query = f"SELECT id, mobile, name, fname, address, alt, circle, email FROM users WHERE {search_field} = ? LIMIT 5"
        try:
            cursor.execute(query, (text,))
            desc = cursor.getdescription()
            results = [{col[0]: r[i] for i, col in enumerate(desc)} for r in cursor]
        except Exception as e:
            await send_tg_msg_async(bot_token, chat_id, f"❌ Database Error: {e}")
            c.close(); conn.close()
            return {"ok": True}

        found = len(results) > 0
        status_str = "Found" if found else "Not Found"
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute("UPDATE api_keys SET used_count = used_count + 1, last_request_time = %s WHERE api_key = %s", (now_str, api_key))
        c.execute("INSERT INTO search_logs (api_key, number, status, timestamp, ip_address) VALUES (%s, %s, %s, %s, %s)", (api_key, text, status_str, now_str, f"TG-{chat_id}"))
        conn.commit()
        
        b_credits = get_credits("bot")
        credit_text = f"━━━━━━━━━━━━━━━\n🤖 <b>Credits:</b>\n👨‍💻 Developer: {b_credits.get('developer', 'N/A')}\n👤 Name: {b_credits.get('name', 'N/A')}\n📱 Telegram: {b_credits.get('telegram', 'N/A')}"

        if found:
            reply = f"✅ <b>Results for {text}:</b>\n\n"
            for r in results:
                reply += f"🆔 ID: <code>{r['id']}</code>\n📞 Mobile: <code>{r['mobile']}</code>\n📛 Name: {r['name']}\n👨‍👦 Fname: {r['fname']}\n📱 Alt: {r['alt']}\n🌍 Circle: {r['circle']}\n📍 Address: {r['address']}\n📧 Email: {r['email']}\n\n"
            reply += credit_text
        else:
            reply = f"❌ No results found for <b>{text}</b>.\n\n{credit_text}"

        await send_tg_msg_async(bot_token, chat_id, reply)
        c.close(); conn.close()
        
    except Exception as e:
        print(f"Webhook Error: {e}")
    return {"ok": True}

# ==========================================
# 🔐 AUTHENTICATION
# ==========================================
def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    conn = get_admin_db(); c = conn.cursor()
    c.execute("SELECT password FROM admins WHERE username = %s", (credentials.username,))
    row = c.fetchone()
    c.close(); conn.close()
    if not row or not secrets.compare_digest(hash_pass(credentials.password), row[0]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Credentials", headers={"WWW-Authenticate": "Basic"})
    return {"username": credentials.username}

# ==========================================
# 🌐 PUBLIC STATUS & CLIENT PORTAL
# ==========================================
@app.get("/status", response_class=HTMLResponse)
async def status_page():
    is_maint = get_setting("maintenance") == "1"
    status_color = "#ff477e" if is_maint else "#00ffad"
    status_text = "MAINTENANCE MODE" if is_maint else "SYSTEM ONLINE"
    return f"""<html><head><title>API Status</title><style>body{{background:#02080b;color:#e2faff;font-family:sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;text-align:center;}} .box{{border:1px solid #10474a;padding:40px;border-radius:16px;background:#061318;}} h1{{color:{status_color};}}</style></head><body><div class="box"><h1>{status_text}</h1><p>API is currently {status_text.lower()}.</p></div></body></html>"""

@app.get("/client-portal", response_class=HTMLResponse)
async def client_portal():
    return """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Client Portal</title><style>body{background:#0f172a;color:#e2e8f0;font-family:sans-serif;display:flex;justify-content:center;padding:40px;} .box{background:#1e293b;padding:30px;border-radius:12px;width:100%;max-width:600px;} input{width:100%;padding:12px;margin:10px 0;border-radius:6px;border:1px solid #334155;background:#0f172a;color:white;} button{width:100%;padding:12px;background:#3b82f6;color:white;border:none;border-radius:6px;cursor:pointer;font-weight:bold;} .stat{display:flex;justify-content:space-between;border-bottom:1px solid #334155;padding:8px 0;} table{width:100%;margin-top:20px;border-collapse:collapse;font-size:12px;} th,td{padding:8px;border-bottom:1px solid #334155;text-align:left;}</style></head><body><div class="box" id="loginBox"><h2>Client Login</h2><input type="text" id="apiKeyInput" placeholder="Enter your API Key (e.g. HTK-XXXX)"><button onclick="login()">Login</button></div><div class="box" id="dashboardBox" style="display:none;"><h2>My Dashboard</h2><div id="info"></div><h3>Recent Searches</h3><table><thead><tr><th>Time</th><th>Query</th><th>Status</th></tr></thead><tbody id="logs"></tbody></table><button onclick="logout()" style="margin-top:20px;background:#ef4444;">Logout</button></div><script>const apiKey = () => localStorage.getItem("client_key");async function login() {const k = document.getElementById("apiKeyInput").value.trim();if(!k) return alert("Key daalo");const res = await fetch(`/api/client/dashboard?key=${k}`);if(res.ok) { localStorage.setItem("client_key", k); loadDashboard(); } else { alert("Invalid API Key"); }}function logout() { localStorage.removeItem("client_key"); location.reload(); }async function loadDashboard() {const k = apiKey(); if(!k) return;const res = await fetch(`/api/client/dashboard?key=${k}`);if(!res.ok) { logout(); return; }const data = await res.json();document.getElementById("loginBox").style.display = "none";document.getElementById("dashboardBox").style.display = "block";document.getElementById("info").innerHTML = `<div class="stat"><span>Key</span><strong>${data.key}</strong></div><div class="stat"><span>Type</span><strong>${data.type.toUpperCase()}</strong></div><div class="stat"><span>Expiry</span><strong>${data.expiry}</strong></div><div class="stat"><span>Used / Limit</span><strong>${data.used} / ${data.limit}</strong></div>`;document.getElementById("logs").innerHTML = data.logs.map(l => `<tr><td>${l.time}</td><td>${l.number}</td><td>${l.status}</td></tr>`).join("") || "<tr><td colspan='3'>No logs yet.</td></tr>";}if(apiKey()) loadDashboard();</script></body></html>"""

@app.get("/api/client/dashboard")
async def client_dashboard(key: str = Query(...)):
    conn = get_admin_db(); c = conn.cursor()
    c.execute("SELECT expiry_date, max_limit, used_count, key_type FROM api_keys WHERE api_key = %s AND is_active = 1", (key,))
    row = c.fetchone()
    if not row: c.close(); conn.close(); raise HTTPException(status_code=404, detail="Invalid or blocked key")
    c.execute("SELECT number, status, timestamp FROM search_logs WHERE api_key = %s ORDER BY id DESC LIMIT 10", (key,))
    logs = [{"number": l[0], "status": l[1], "time": l[2]} for l in c.fetchall()]
    c.close(); conn.close()
    return {"key": key, "expiry": row[0], "limit": row[1], "used": row[2], "type": row[3], "logs": logs}

# ==========================================
# 📊 ADMIN API
# ==========================================
@app.get("/api/admin/dashboard")
async def get_dashboard_data(admin: dict = Depends(verify_admin)):
    conn = get_admin_db(); c = conn.cursor()
    c.execute("SELECT * FROM api_keys ORDER BY created_date DESC")
    rows = c.fetchall()
    c.execute("SELECT * FROM search_logs ORDER BY id DESC LIMIT 100")
    logs_rows = c.fetchall()
    c.execute("SELECT * FROM bots ORDER BY bot_id DESC")
    bots_rows = c.fetchall()
    c.execute("SELECT * FROM bot_users ORDER BY joined_date DESC")
    bot_users_rows = c.fetchall()
    c.execute("SELECT timestamp FROM search_logs")
    all_logs = c.fetchall()
    c.close(); conn.close()

    keys_list = []
    for r in rows:
        key, owner, expiry, limit, used, active, created, rate_limit, last_req, allowed_ip, key_type = r[:11]
        status_str = "active" if active else "blocked"
        try:
            if datetime.strptime(expiry, "%Y-%m-%d").date() < datetime.now().date(): status_str = "expired"
        except: pass
        keys_list.append({"id": key, "key": key, "owner": owner, "expiry": expiry, "limit": limit, "used": used, "status": status_str, "created": created, "rate_limit": rate_limit, "allowed_ip": allowed_ip, "key_type": key_type})

    logs_list = [{"id": l[0], "key": l[1], "number": l[2], "status": l[3], "time": l[4], "ip": l[5]} for l in logs_rows]
    bots_list = [{"id": b[0], "name": b[1], "token": b[2], "is_active": b[3], "created": b[4]} for b in bots_rows]
    bot_users_list = [{"tg_id": u[0], "bot_token": u[1], "api_key": u[2], "username": u[3], "is_blocked": u[4], "joined": u[5]} for u in bot_users_rows]

    chart_data = [0]*7
    today = datetime.now().date()
    for log in all_logs:
        try:
            diff = (today - datetime.strptime(log[0][:10], "%Y-%m-%d").date()).days
            if 0 <= diff < 7: chart_data[6 - diff] += 1
        except: pass

    return {
        "stats": {"total": len(keys_list), "active": sum(1 for k in keys_list if k["status"] == "active"), "requests": sum(k["used"] for k in keys_list), "blocked": sum(1 for k in keys_list if k["status"] != "active")},
        "keys": keys_list, "logs": logs_list, "bots": bots_list, "bot_users": bot_users_list, "chart_data": chart_data,
        "settings": {"maintenance": get_setting("maintenance"), "global_rate_limit": get_setting("global_rate_limit"), "admin_chat_id": get_setting("admin_chat_id"), "api_credits": get_credits("api"), "bot_credits": get_credits("bot")}
    }

@app.post("/api/admin/generate")
async def generate_key(owner: str = Form(...), days: int = Form(...), limit: int = Form(...), key_type: str = Form("mobile"), allowed_ip: str = Form(""), admin: dict = Depends(verify_admin)):
    new_key = f"HTK-{secrets.token_hex(4).upper()}"
    expiry_date = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    created_date = datetime.now().strftime("%Y-%m-%d")
    rate_limit = int(get_setting("global_rate_limit") or 1)
    clean_ip = allowed_ip.strip() if allowed_ip.strip() else None
    conn = get_admin_db(); c = conn.cursor()
    c.execute("INSERT INTO api_keys (api_key, owner_name, expiry_date, max_limit, created_date, rate_limit_seconds, allowed_ip, key_type) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", (new_key, owner, expiry_date, limit, created_date, rate_limit, clean_ip, key_type))
    conn.commit(); c.close(); conn.close()
    return {"success": True, "key": new_key}

class ActionPayload(BaseModel):
    key: str
    action: str
    value: Optional[str] = None

@app.post("/api/admin/action")
async def admin_action(payload: ActionPayload, admin: dict = Depends(verify_admin)):
    conn = get_admin_db(); c = conn.cursor()
    if payload.action == "extend":
        c.execute("SELECT expiry_date FROM api_keys WHERE api_key = %s", (payload.key,))
        row = c.fetchone()
        if row:
            try: new_date = (datetime.strptime(row[0], "%Y-%m-%d") + timedelta(days=30)).strftime("%Y-%m-%d")
            except: new_date = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
            c.execute("UPDATE api_keys SET expiry_date = %s, expiry_alert_sent = 0 WHERE api_key = %s", (new_date, payload.key))
    elif payload.action == "increase": c.execute("UPDATE api_keys SET max_limit = max_limit + 500 WHERE api_key = %s", (payload.key,))
    elif payload.action == "edit":
        try:
            new_limit, new_expiry, new_ip = payload.value.split("|")
            new_ip = new_ip.strip() if new_ip.strip() else None
            c.execute("UPDATE api_keys SET max_limit = %s, expiry_date = %s, allowed_ip = %s, expiry_alert_sent = 0 WHERE api_key = %s", (int(new_limit), new_expiry, new_ip, payload.key))
        except: pass
    elif payload.action == "block": c.execute("UPDATE api_keys SET is_active = 0 WHERE api_key = %s", (payload.key,))
    elif payload.action == "unblock": c.execute("UPDATE api_keys SET is_active = 1 WHERE api_key = %s", (payload.key,))
    elif payload.action == "delete": c.execute("DELETE FROM api_keys WHERE api_key = %s", (payload.key,))
    conn.commit(); c.close(); conn.close()
    return {"success": True}

class BotPayload(BaseModel):
    name: str
    token: str

@app.post("/api/admin/add_bot")
async def add_bot(payload: BotPayload, request: Request, admin: dict = Depends(verify_admin)):
    token = payload.token.strip()
    name = payload.name.strip()
    conn = get_admin_db(); c = conn.cursor()
    try:
        c.execute("INSERT INTO bots (bot_name, bot_token, created_at) VALUES (%s, %s, %s)", (name, token, datetime.now().strftime("%Y-%m-%d")))
        conn.commit()
        base_url = str(request.base_url).rstrip("/")
        webhook_url = f"{base_url}/webhook/{token}"
        requests.get(f"https://api.telegram.org/bot{token}/setWebhook?url={webhook_url}")
        success = True
    except Exception as e:
        print(f"Add Bot Error: {e}")
        success = False
    c.close(); conn.close()
    return {"success": success}

class BotActionPayload(BaseModel):
    token: str
    action: str

@app.post("/api/admin/bot_action")
async def bot_action(payload: BotActionPayload, admin: dict = Depends(verify_admin)):
    conn = get_admin_db(); c = conn.cursor()
    if payload.action == "toggle": c.execute("UPDATE bots SET is_active = 1 - is_active WHERE bot_token = %s", (payload.token,))
    elif payload.action == "delete": 
        c.execute("DELETE FROM bots WHERE bot_token = %s", (payload.token,))
        c.execute("DELETE FROM bot_users WHERE bot_token = %s", (payload.token,))
    conn.commit(); c.close(); conn.close()
    return {"success": True}

class UserActionPayload(BaseModel):
    tg_id: str
    bot_token: str
    action: str

@app.post("/api/admin/bot_user_action")
async def bot_user_action(payload: UserActionPayload, admin: dict = Depends(verify_admin)):
    conn = get_admin_db(); c = conn.cursor()
    if payload.action == "block": c.execute("UPDATE bot_users SET is_blocked = 1 WHERE telegram_id = %s AND bot_token = %s", (payload.tg_id, payload.bot_token))
    elif payload.action == "unblock": c.execute("UPDATE bot_users SET is_blocked = 0 WHERE telegram_id = %s AND bot_token = %s", (payload.tg_id, payload.bot_token))
    elif payload.action == "remove_key": c.execute("UPDATE bot_users SET api_key = NULL WHERE telegram_id = %s AND bot_token = %s", (payload.tg_id, payload.bot_token))
    conn.commit(); c.close(); conn.close()
    return {"success": True}

class MessagePayload(BaseModel):
    bot_token: str
    tg_id: Optional[str] = None
    message: str

@app.post("/api/admin/send_message")
async def send_message(payload: MessagePayload, admin: dict = Depends(verify_admin)):
    if payload.tg_id:
        await send_tg_msg_async(payload.bot_token, payload.tg_id, payload.message)
        return {"success": True, "sent": 1}
    else:
        conn = get_admin_db(); c = conn.cursor()
        c.execute("SELECT telegram_id FROM bot_users WHERE bot_token = %s AND is_blocked = 0", (payload.bot_token,))
        users = c.fetchall()
        c.close(); conn.close()
        for u in users: await send_tg_msg_async(payload.bot_token, u[0], payload.message)
        return {"success": True, "sent": len(users)}

class SettingsPayload(BaseModel):
    maintenance: str
    global_rate_limit: str
    admin_chat_id: str

@app.post("/api/admin/settings")
async def update_settings(payload: SettingsPayload, admin: dict = Depends(verify_admin)):
    for k, v in payload.dict().items(): set_setting(k, v)
    return {"success": True}

class CreditsPayload(BaseModel):
    type: str
    developer: str
    name: str
    telegram: str

@app.post("/api/admin/save_credits")
async def save_credits(payload: CreditsPayload, admin: dict = Depends(verify_admin)):
    new_credits = {"developer": payload.developer, "name": payload.name, "telegram": payload.telegram}
    set_setting(f"{payload.type}_credits", json.dumps(new_credits))
    return {"success": True}

# ==========================================
# 🖥️ ADMIN HTML
# ==========================================
ADMIN_HTML = """
<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Admin Panel</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
:root{--bg:#02080b;--panel:#061318;--line:#0a4949;--green:#00ffad;--cyan:#00e5ff;--text:#e2faff;--muted:#86aebc;--red:#ff477e;--radius:16px;}
*{box-sizing:border-box} body{margin:0;background:radial-gradient(ellipse at 50% 0%,#06242a 0,#02080b 55%,#010507 100%);color:var(--text);font-family:Inter,sans-serif;min-height:100vh;}
button,input,select,textarea{font:inherit} button{cursor:pointer}
.app{display:grid;grid-template-columns:250px minmax(0,1fr);min-height:100vh}
.sidebar{position:sticky;top:0;height:100vh;padding:26px 16px;border-right:1px solid #0b353c;background:rgba(2,12,16,.92);display:flex;flex-direction:column;gap:28px;z-index:100;}
.brand{display:flex;align-items:center;gap:12px;padding:0 10px 22px;border-bottom:1px solid #10383d}
.logo{width:42px;height:42px;display:grid;place-items:center;color:#001c18;background:var(--green);font-size:24px;font-weight:900;}
.brand strong{font-size:15px;color:var(--green);display:block}
.nav{display:grid;gap:8px}.nav button{display:flex;align-items:center;gap:13px;padding:13px 14px;border:1px solid transparent;border-radius:10px;background:transparent;color:#9bbfc8;text-align:left}
.nav button:hover,.nav button.active{border-color:#087e71;background:linear-gradient(90deg,#05372f,#06232a);color:var(--green);box-shadow:inset 3px 0 var(--green);}
main{padding:28px 42px;width:100%}.topbar{display:flex;align-items:center;justify-content:space-between;margin-bottom:28px}
h1{font-size:24px;margin:0}.subtitle{color:var(--muted);font-size:14px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:15px;margin-bottom:22px}
.stat{border:1px solid #10474a;border-radius:var(--radius);padding:18px;background:linear-gradient(145deg,#071d22,#041015);}
.stat .stat-icon{font-size:21px;color:var(--green)}.stat label{color:#8bb9c2;font-size:12px;margin-top:14px;display:block}
.stat strong{font-size:28px;margin:3px 0}.stat small{color:var(--green);font-size:11px}
.content-grid{display:grid;grid-template-columns:1.1fr .9fr;gap:20px;align-items:start}
.card{border:1px solid #10474a;border-radius:var(--radius);background:linear-gradient(150deg,rgba(6,27,32,.97),rgba(3,13,18,.98));margin-bottom:20px;}
.card-head{padding:19px 21px;border-bottom:1px solid #10363d;display:flex;justify-content:space-between;align-items:center;}
.card-head h2{font-size:17px;margin:0;color:var(--green)}.card-body{padding:20px}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:17px}.field{display:grid;gap:8px}.field.full{grid-column:1/-1}
.field label{font-size:12px;color:#a8d2da}.field input,.field select,.search{width:100%;padding:13px;color:var(--text);background:#020d12;border:1px solid #15515a;border-radius:10px;outline:none}
.btn{border:1px solid transparent;border-radius:10px;padding:13px;font-weight:700;color:#00150f;background:var(--green);}.btn.full{width:100%;grid-column:1/-1}
.btn.secondary{background:#09232b;color:#b7e9ef;border-color:#15515a}.btn.danger{background:#390d20;color:#ff8eae;}
.key-row{display:grid;grid-template-columns:35px 1fr auto 28px;gap:10px;padding:14px 4px;border-bottom:1px solid #0c2c33;align-items:center;}
.key-main strong{font:12px monospace;display:block}.key-main small{color:#83abb8;font-size:11px}
.status{font-size:10px;padding:6px 9px;border-radius:20px;font-weight:700;background:#063c30;color:var(--green)}
.status.expired{background:#451026;color:#ff7aa3}.status.blocked{background:#132b45;color:#8cc8ff}
.log-table{width:100%;border-collapse:collapse}.log-table th{text-align:left;padding:10px;color:var(--muted);font-size:12px;border-bottom:1px solid #10363d;}.log-table td{padding:10px;font-size:12px;border-bottom:1px solid #0c2c33;}
.switch{position:relative;display:inline-block;width:46px;height:24px;}.switch input{opacity:0;width:0;height:0;}.slider{position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;background-color:#2a3b45;transition:.4s;border-radius:24px;}.slider:before{position:absolute;content:"";height:18px;width:18px;left:3px;bottom:3px;background-color:white;transition:.4s;border-radius:50%;}input:checked + .slider{background-color:var(--green);}input:checked + .slider:before{transform:translateX(22px);}
.page-section{display:none;}.page-section.active{display:block;}
.toast{position:fixed;right:22px;bottom:22px;padding:14px 18px;border:1px solid var(--green);border-radius:12px;background:#041b18;color:#caffed;display:none;z-index:200}.toast.show{display:block;}
@media (max-width: 900px){.app{display:block}.sidebar{position:fixed;left:-270px;width:250px;transition:left .3s;z-index:999;height:100vh}.sidebar.open{left:0}.stats{grid-template-columns:1fr 1fr}.content-grid{grid-template-columns:1fr}.form-grid{grid-template-columns:1fr}main{padding:15px}}
.mobile-menu{display:none;background:none;border:none;color:white;font-size:24px}
@media(max-width:900px){.mobile-menu{display:block}}
</style></head>
<body>
<div class="app">
  <aside class="sidebar" id="sidebar">
    <div class="brand"><div class="logo">ϟ</div><div><strong>Insta DB API</strong><small>SUPERADMIN</small></div></div>
    <nav class="nav">
      <button class="active" onclick="showPage('dashboard', this)">⌂ Dashboard</button>
      <button onclick="showPage('keys', this)">⚿ API Keys</button>
      <button onclick="showPage('generate', this)">⊕ Generate Key</button>
      <button onclick="showPage('logs', this)">📄 Search Logs</button>
      <button onclick="showPage('bots', this)">🤖 Bot Manager</button>
      <button onclick="showPage('credits', this)">👨‍💻 Credits</button>
      <button onclick="showPage('settings', this)">⚙ Settings</button>
    </nav>
  </aside>
  <main>
    <header class="topbar">
      <div style="display:flex;gap:12px;align-items:center">
        <button class="mobile-menu" onclick="document.getElementById('sidebar').classList.toggle('open')">☰</button>
        <div><h1>Insta Database API</h1><p class="subtitle">Multi-Bot Control System</p></div>
      </div>
      <div style="display:flex; gap:10px;">
        <a href="/client-portal" target="_blank" class="btn secondary" style="text-decoration:none;">👤 Client Portal</a>
        <button class="btn secondary" onclick="loadData()">↻ Refresh</button>
      </div>
    </header>

    <div id="page-dashboard" class="page-section active">
        <section class="stats">
        <article class="stat"><div class="stat-icon">⚿</div><label>Total Keys</label><strong id="totalStat">0</strong><small>↗ Created</small></article>
        <article class="stat"><div class="stat-icon">ϟ</div><label>Active</label><strong id="activeStat">0</strong><small>● Enabled</small></article>
        <article class="stat"><div class="stat-icon">⇄</div><label>Requests</label><strong id="requestStat">0</strong><small>↗ All time</small></article>
        <article class="stat" style="border-color:#5c2038"><div class="stat-icon" style="color:var(--red)">⊘</div><label>Blocked</label><strong id="blockedStat">0</strong><small style="color:var(--red)">● Restricted</small></article>
        </section>
        <article class="card"><div class="card-head"><h2>📈 Last 7 Days Analytics</h2></div><div class="card-body"><canvas id="usageChart" height="100"></canvas></div></article>
    </div>

    <div id="page-generate" class="page-section">
        <article class="card"><div class="card-head"><h2>⌁ Generate New API Key</h2></div>
          <div class="card-body"><form id="keyForm" class="form-grid">
              <div class="field"><label>Owner name</label><input id="owner" placeholder="e.g. John Doe" required></div>
              <div class="field"><label>Search Type</label>
                <select id="key_type">
                    <option value="mobile">Mobile Only (Indexed)</option>
                    <option value="id">ID Only (Indexed)</option>
                    <option value="universal" style="color:var(--green); font-weight:bold;">⭐ Universal (Mobile & ID)</option>
                </select>
              </div>
              <div class="field"><label>Valid days</label><select id="expiry"><option value="1">1 day</option><option value="7">7 days</option><option value="30" selected>30 days</option><option value="90">90 days</option><option value="365">365 days</option></select></div>
              <div class="field"><label>Max requests</label><select id="limit"><option value="50">50</option><option value="100" selected>100</option><option value="500">500</option><option value="1000">1,000</option><option value="5000">5,000</option></select></div>
              <div class="field full"><label>Allowed IP (Optional)</label><input id="allowed_ip" placeholder="e.g. 192.168.1.1"></div>
              <button class="btn full" type="submit" id="genBtn">ϟ GENERATE API KEY</button>
          </form></div>
        </article>
    </div>

    <div id="page-keys" class="page-section"><div class="content-grid">
          <section><article class="card"><div class="card-head"><h2>⌘ Managed Keys</h2><input class="search" id="searchInput" placeholder="Search..." style="width:150px"></div><div class="card-body" id="keyList"></div></article></section>
          <section><article class="card"><div class="card-head"><h2>◈ Key Intelligence</h2></div><div class="card-body" id="keyDetails"><div style="text-align:center; color:var(--muted)">Select a key.</div></div></article></section>
    </div></div>

    <div id="page-logs" class="page-section"><article class="card"><div class="card-head"><h2>📄 Search Logs</h2></div><div class="card-body" style="overflow-x:auto;"><table class="log-table"><thead><tr><th>Time</th><th>Key</th><th>Query</th><th>IP</th><th>Status</th></tr></thead><tbody id="logsBody"></tbody></table></div></article></div>

    <div id="page-bots" class="page-section">
        <div class="content-grid">
            <section>
                <article class="card">
                    <div class="card-head"><h2>➕ Add New Bot</h2></div>
                    <div class="card-body">
                        <form id="addBotForm" class="form-grid">
                            <div class="field"><label>Bot Name</label><input id="botName" placeholder="e.g. My Insta Bot" required></div>
                            <div class="field"><label>Bot Token</label><input id="botToken" placeholder="From BotFather" required></div>
                            <button class="btn full" type="submit">ADD BOT</button>
                        </form>
                    </div>
                </article>
                <article class="card">
                    <div class="card-head"><h2>🤖 Your Bots</h2></div>
                    <div class="card-body" id="botList" style="max-height:400px; overflow-y:auto;"></div>
                </article>
            </section>
            <section>
                <article class="card">
                    <div class="card-head"><h2>👥 Bot Users</h2></div>
                    <div class="card-body" style="max-height:700px; overflow-y:auto;">
                        <table class="log-table">
                            <thead><tr><th>User</th><th>Key</th><th>Action</th></tr></thead>
                            <tbody id="botUsersBody"></tbody>
                        </table>
                    </div>
                </article>
            </section>
        </div>
    </div>

    <div id="page-credits" class="page-section">
        <div class="content-grid">
            <section>
                <article class="card">
                    <div class="card-head"><h2>🔗 API Credits</h2></div>
                    <div class="card-body">
                        <form id="apiCreditsForm" class="form-grid">
                            <div class="field full"><label>Developer Name</label><input id="apiDev" required></div>
                            <div class="field full"><label>Your Name</label><input id="apiName" required></div>
                            <div class="field full"><label>Telegram Username</label><input id="apiTg" required></div>
                            <button class="btn full" type="submit">SAVE API CREDITS</button>
                        </form>
                    </div>
                </article>
            </section>
            <section>
                <article class="card">
                    <div class="card-head"><h2>🤖 Bot Credits</h2></div>
                    <div class="card-body">
                        <form id="botCreditsForm" class="form-grid">
                            <div class="field full"><label>Developer Name</label><input id="botDev" required></div>
                            <div class="field full"><label>Your Name</label><input id="botNameInp" required></div>
                            <div class="field full"><label>Telegram Username</label><input id="botTg" required></div>
                            <button class="btn full" type="submit">SAVE BOT CREDITS</button>
                        </form>
                    </div>
                </article>
            </section>
        </div>
    </div>

    <div id="page-settings" class="page-section"><article class="card"><div class="card-head"><h2>⚙ Global Settings</h2></div>
            <div class="card-body"><form id="settingsForm">
                    <div class="field" style="flex-direction:row; justify-content:space-between; margin-bottom:20px;"><div><label>API Maintenance Mode</label></div><label class="switch"><input type="checkbox" id="setMaintenance"><span class="slider"></span></label></div>
                    <div class="field" style="margin-bottom:20px;"><label>API Rate Limit (Secs)</label><input type="number" id="setRateLimit" class="search" required></div>
                    <div style="border-top:1px solid #10363d; padding-top:20px;">
                        <h3 style="color:var(--green)">Admin Alerts</h3>
                        <div class="field" style="margin-bottom:15px;"><label>Admin Chat ID (Telegram)</label><input type="text" id="setAdminChatId" class="search"></div>
                    </div>
                    <button type="submit" class="btn full">SAVE SETTINGS</button>
            </form></div>
    </article></div>
  </main>
</div>

<div id="broadcastModal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.8); z-index:1000; justify-content:center; align-items:center;">
    <div class="card" style="width:400px;">
        <div class="card-head"><h2>📢 Broadcast Message</h2></div>
        <div class="card-body">
            <p style="font-size:12px; color:var(--muted); margin-bottom:10px;">Bot: <span id="bcBotName" style="color:var(--cyan)"></span></p>
            <textarea id="bcMsg" class="search" rows="4" placeholder="Message likho..."></textarea>
            <div style="display:flex; gap:10px; margin-top:15px;">
                <button class="btn full" onclick="sendBroadcast()">Send to All Users</button>
                <button class="btn secondary full" onclick="closeModal()">Cancel</button>
            </div>
        </div>
    </div>
</div>

<div id="dmModal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.8); z-index:1000; justify-content:center; align-items:center;">
    <div class="card" style="width:400px;">
        <div class="card-head"><h2>💬 Send Direct Message</h2></div>
        <div class="card-body">
            <p style="font-size:12px; color:var(--muted); margin-bottom:10px;">To: <span id="dmUser" style="color:var(--cyan)"></span></p>
            <textarea id="dmMsg" class="search" rows="4" placeholder="Message likho..."></textarea>
            <div style="display:flex; gap:10px; margin-top:15px;">
                <button class="btn full" onclick="sendDirectMsg()">Send Message</button>
                <button class="btn secondary full" onclick="closeModal()">Cancel</button>
            </div>
        </div>
    </div>
</div>

<div class="toast" id="toast"></div>
<script>
let keys = [], logs = [], bots = [], bot_users = [], currentSettings = {}, chartInstance = null;
let selectedBotForBC = null, selectedUserForDM = null;
const $ = id => document.getElementById(id);
function showPage(pageId, btn) { 
    document.querySelectorAll('.page-section').forEach(p => p.classList.remove('active')); 
    $('page-' + pageId).classList.add('active'); 
    if(btn) { document.querySelectorAll('.nav button').forEach(b => b.classList.remove('active')); btn.classList.add('active'); } 
    if(pageId === 'settings') loadSettingsUI(); 
    if(pageId === 'credits') loadCreditsUI();
    if(window.innerWidth <= 900) $('sidebar').classList.remove('open'); 
}
function toast(message){ const t=$("toast"); t.textContent=message; t.classList.add("show"); setTimeout(()=>t.classList.remove("show"),3200); }
function escapeHtml(s){ return String(s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

function loadSettingsUI() { $("setMaintenance").checked = currentSettings.maintenance === "1"; $("setRateLimit").value = currentSettings.global_rate_limit || "1"; $("setAdminChatId").value = currentSettings.admin_chat_id || ""; }

function loadCreditsUI() {
    if(currentSettings.api_credits) {
        $("apiDev").value = currentSettings.api_credits.developer || "";
        $("apiName").value = currentSettings.api_credits.name || "";
        $("apiTg").value = currentSettings.api_credits.telegram || "";
    }
    if(currentSettings.bot_credits) {
        $("botDev").value = currentSettings.bot_credits.developer || "";
        $("botNameInp").value = currentSettings.bot_credits.name || "";
        $("botTg").value = currentSettings.bot_credits.telegram || "";
    }
}

function updateStats(stats){ $("totalStat").textContent = stats.total; $("activeStat").textContent = stats.active; $("blockedStat").textContent = stats.blocked; $("requestStat").textContent = stats.requests.toLocaleString(); }
function renderChart(data) { if(chartInstance) chartInstance.destroy(); const ctx = document.getElementById('usageChart').getContext('2d'); chartInstance = new Chart(ctx, { type: 'line', data: { labels: ['6d','5d','4d','3d','2d','1d','Today'], datasets: [{ data: data, borderColor: '#00ffad', backgroundColor: 'rgba(0, 255, 173, 0.1)', borderWidth: 2, fill: true, tension: 0.4 }] }, options: { plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true }, x: { grid: { display: false } } } } }); }
function renderKeys(){
    const query=$("searchInput").value.trim().toLowerCase();
    const shown=keys.filter(k=>(`${k.key} ${k.owner} ${k.status}`.toLowerCase().includes(query)));
    $("keyList").innerHTML = shown.length ? shown.map(k=>`<div class="key-row"><div class="stat-icon">⚿</div><div class="key-main"><strong>${escapeHtml(k.key)}</strong><small>${escapeHtml(k.owner)} • ${k.used}/${k.limit} • ${k.key_type.toUpperCase()}</small></div><span class="status ${k.status}">${k.status.toUpperCase()}</span><button class="btn secondary" onclick="selectKey('${k.key}')">›</button></div>`).join("") : `<p style="color:var(--muted)">No keys found.</p>`;
}
function selectKey(id){
    const k=keys.find(x=>x.key===id); if(!k) return;
    let actionButtons = `<button class="btn" onclick="performAction('${k.key}', 'extend')">Extend 30 Days</button><button class="btn secondary" onclick="performAction('${k.key}', 'increase')">Increase Limit</button><button class="btn secondary" onclick="editKey('${k.key}', ${k.limit}, '${k.expiry}', '${k.allowed_ip||""}')">Edit Settings</button><button class="btn ${k.status==="active"?"danger":"secondary"}" onclick="performAction('${k.key}', '${k.status==="active"?"block":"unblock"}')">${k.status==="active"?"Block":"Unblock"}</button><button class="btn danger" onclick="deleteKey('${k.key}')">DELETE KEY</button>`;
    $("keyDetails").innerHTML = `<div class="detail-top"><h3>${k.key}</h3><span class="status ${k.status}">${k.status.toUpperCase()}</span></div><p><b>Owner:</b> ${k.owner}</p><p><b>Type:</b> ${k.key_type.toUpperCase()}</p><p><b>Expiry:</b> ${k.expiry}</p><p><b>Used:</b> ${k.used} / ${k.limit}</p><div class="actions" style="display:grid; gap:8px;">${actionButtons}</div>`;
}
function editKey(key, currentLimit, currentExpiry, currentIp) {
    const newLimit = prompt("Enter new Max Request Limit:", currentLimit); if(newLimit === null) return;
    const newExpiry = prompt("Enter new Expiry Date (YYYY-MM-DD):", currentExpiry); if(newExpiry === null) return;
    const newIp = prompt("Enter Allowed IP (Leave blank for any):", currentIp || ""); if(newIp === null) return;
    performAction(key, 'edit', newLimit + "|" + newExpiry + "|" + newIp);
}
function deleteKey(key) { if(confirm("Permanently delete this API key?")) { performAction(key, "delete"); $("keyDetails").innerHTML = 'Select a key.'; } }
async function performAction(key, action, value=null) { try { const res = await fetch('/api/admin/action', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({key, action, value}) }); if(res.ok) { toast("Success!"); await loadData(); } } catch(e) {} }

function renderBots() {
    $("botList").innerHTML = bots.map(b => `
        <div class="key-row" style="grid-template-columns: 1fr auto; padding: 10px; border-bottom: 1px solid #10363d;">
            <div><strong style="color:var(--cyan)">${escapeHtml(b.name)}</strong><div style="font-size:10px; color:var(--muted); margin-top:3px;">Token: ${b.token.substring(0, 15)}...</div></div>
            <div style="display:flex; gap:5px; align-items:center;">
                <button class="btn ${b.is_active ? 'danger' : ''}" style="padding:6px 10px; font-size:10px;" onclick="toggleBot('${b.token}')">${b.is_active ? 'Turn OFF' : 'Turn ON'}</button>
                <button class="btn secondary" style="padding:6px 10px; font-size:10px;" onclick="openBroadcast('${b.token}', '${escapeHtml(b.name)}')">📢 Broadcast</button>
                <button class="btn danger" style="padding:6px 10px; font-size:10px;" onclick="deleteBot('${b.token}')">✖</button>
            </div>
        </div>
    `).join("") || "<p style='color:var(--muted)'>No bots added yet.</p>";
}
function renderBotUsers() {
    $("botUsersBody").innerHTML = bot_users.map(u => {
        const bot = bots.find(b => b.token === u.bot_token);
        return `<tr><td><b>@${escapeHtml(u.username)}</b><br><small>${u.tg_id}</small><br><small style="color:var(--cyan)">Bot: ${bot ? escapeHtml(bot.name) : 'Unknown'}</small></td><td style="color:${u.api_key ? 'var(--green)' : 'var(--red)'}">${u.api_key || "No Key"}</td><td style="display:flex; flex-direction:column; gap:4px;"><button class="btn ${u.is_blocked ? 'secondary' : 'danger'}" style="padding:4px 8px; font-size:10px;" onclick="botUserAction('${u.tg_id}', '${u.bot_token}', '${u.is_blocked ? 'unblock' : 'block'}')">${u.is_blocked ? 'Unblock' : 'Block'}</button><button class="btn secondary" style="padding:4px 8px; font-size:10px;" onclick="openDM('${u.tg_id}', '${u.bot_token}', '${escapeHtml(u.username)}')">💬 DM</button><button class="btn danger" style="padding:4px 8px; font-size:10px;" onclick="botUserAction('${u.tg_id}', '${u.bot_token}', 'remove_key')">Remove Key</button></td></tr>`
    }).join("") || "<tr><td colspan='3'>No bot users.</td></tr>";
}
async function toggleBot(token) { await fetch('/api/admin/bot_action', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({token, action: 'toggle'}) }); toast("Bot status updated"); await loadData(); }
async function deleteBot(token) { if(!confirm("Delete this bot permanently?")) return; await fetch('/api/admin/bot_action', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({token, action: 'delete'}) }); toast("Bot deleted"); await loadData(); }
async function botUserAction(tg_id, bot_token, action) { if(!confirm("Are you sure?")) return; await fetch('/api/admin/bot_user_action', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({tg_id, bot_token, action}) }); toast("Action done"); await loadData(); }
function openBroadcast(token, name) { selectedBotForBC = token; $("bcBotName").textContent = name; $("bcMsg").value = ""; $("broadcastModal").style.display = "flex"; }
async function sendBroadcast() { const msg = $("bcMsg").value.trim(); if(!msg) return toast("Message khali hai!"); toast("Sending..."); const res = await fetch('/api/admin/send_message', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({bot_token: selectedBotForBC, message: msg}) }); const data = await res.json(); if(data.success) { toast(`Sent to ${data.sent} users!`); closeModal(); } }
function openDM(tg_id, bot_token, username) { selectedUserForDM = { tg_id, bot_token }; $("dmUser").textContent = "@" + username; $("dmMsg").value = ""; $("dmModal").style.display = "flex"; }
async function sendDirectMsg() { const msg = $("dmMsg").value.trim(); if(!msg) return toast("Message khali hai!"); toast("Sending..."); const res = await fetch('/api/admin/send_message', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({bot_token: selectedUserForDM.bot_token, tg_id: selectedUserForDM.tg_id, message: msg}) }); if(res.ok) { toast("Sent!"); closeModal(); } }
function closeModal() { $("broadcastModal").style.display = "none"; $("dmModal").style.display = "none"; }

async function loadData() {
    try {
        const res = await fetch('/api/admin/dashboard'); const data = await res.json();
        keys = data.keys; logs = data.logs; bots = data.bots; bot_users = data.bot_users; currentSettings = data.settings;
        updateStats(data.stats); renderKeys(); renderChart(data.chart_data); renderBots(); renderBotUsers();
        $("logsBody").innerHTML = logs.map(l => `<tr><td style="color:var(--muted)">${l.time}</td><td style="color:var(--cyan)">${l.key}</td><td>${l.number}</td><td style="color:var(--muted)">${l.ip}</td><td style="color:${l.status==='Found'?'var(--green)':'var(--red)'}">${l.status}</td></tr>`).join("");
        if($("page-settings").classList.contains('active')) loadSettingsUI();
        if($("page-credits").classList.contains('active')) loadCreditsUI();
    } catch(e) { toast("Error loading data"); }
}
$("addBotForm").addEventListener("submit", async (e)=>{ e.preventDefault(); const payload = { name: $("botName").value, token: $("botToken").value }; const res = await fetch('/api/admin/add_bot', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) }); if(res.ok) { const r = await res.json(); if(r.success) { toast("Bot Added & Webhook Set!"); $("botName").value=""; $("botToken").value=""; await loadData(); } else { toast("Error: Invalid Token or Duplicate"); } } });
$("keyForm").addEventListener("submit", async (e)=>{ e.preventDefault(); const fd = new FormData(); fd.append("owner", $("owner").value); fd.append("key_type", $("key_type").value); fd.append("days", $("expiry").value); fd.append("limit", $("limit").value); fd.append("allowed_ip", $("allowed_ip").value); const res = await fetch('/api/admin/generate', { method: 'POST', body: fd }); if(res.ok) { const r = await res.json(); toast("Key: " + r.key); await loadData(); } });
$("settingsForm").addEventListener("submit", async (e) => { e.preventDefault(); const payload = { maintenance: $("setMaintenance").checked ? "1" : "0", global_rate_limit: $("setRateLimit").value, admin_chat_id: $("setAdminChatId").value }; await fetch('/api/admin/settings', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) }); toast("Saved!"); await loadData(); });

// Save Credits Forms
$("apiCreditsForm").addEventListener("submit", async (e) => { 
    e.preventDefault(); 
    const payload = { type: "api", developer: $("apiDev").value, name: $("apiName").value, telegram: $("apiTg").value }; 
    const res = await fetch('/api/admin/save_credits', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) }); 
    if(res.ok) { toast("API Credits Saved!"); await loadData(); } 
});
$("botCreditsForm").addEventListener("submit", async (e) => { 
    e.preventDefault(); 
    const payload = { type: "bot", developer: $("botDev").value, name: $("botNameInp").value, telegram: $("botTg").value }; 
    const res = await fetch('/api/admin/save_credits', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) }); 
    if(res.ok) { toast("Bot Credits Saved!"); await loadData(); } 
});

$("searchInput").addEventListener("input",renderKeys); loadData();
</script></body></html>
"""

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(admin: dict = Depends(verify_admin)):
    return HTMLResponse(content=ADMIN_HTML)

# ==========================================
# 🔍 MAIN API ROUTE (Search Logic - Mobile & ID)
# ==========================================
@app.get("/search")
async def search_user_data(key: str = Query(...), id: str = Query(None), mobile: str = Query(None), request: Request = None):
    if get_setting("maintenance") == "1": return JSONResponse({"error": "Server is under maintenance."}, status_code=503)

    client_ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "Unknown").split(",")[0].strip()

    conn = get_admin_db(); c = conn.cursor()
    c.execute("SELECT expiry_date, max_limit, used_count, is_active, rate_limit_seconds, last_request_time, allowed_ip, key_type FROM api_keys WHERE api_key = %s", (key,))
    row = c.fetchone()

    if not row: c.close(); conn.close(); return JSONResponse({"error": "Invalid API Key", "credits": get_credits("api")}, status_code=401)

    expiry_str, max_limit, used_count, is_active, rate_limit, last_req_str, allowed_ip, key_type = row

    if allowed_ip and allowed_ip.strip() != "" and client_ip != allowed_ip.strip(): c.close(); conn.close(); return JSONResponse({"error": f"Access Denied. IP mismatch.", "credits": get_credits("api")}, status_code=403)
    if not is_active: c.close(); conn.close(); return JSONResponse({"error": "API Key is blocked by Admin", "credits": get_credits("api")}, status_code=403)
    if datetime.strptime(expiry_str, "%Y-%m-%d").date() < datetime.now().date(): c.close(); conn.close(); return JSONResponse({"error": "API Key Expired", "credits": get_credits("api")}, status_code=403)
    if used_count >= max_limit: c.close(); conn.close(); return JSONResponse({"error": "API Limit Exceeded", "credits": get_credits("api")}, status_code=429)

    if last_req_str:
        last_req_time = datetime.strptime(last_req_str, "%Y-%m-%d %H:%M:%S")
        if (datetime.now() - last_req_time).total_seconds() < rate_limit:
            c.close(); conn.close()
            return JSONResponse({"error": f"Rate limit exceeded. Wait {rate_limit}s.", "credits": get_credits("api")}, status_code=429)

    if key_type == "universal":
        if not (id or mobile): return JSONResponse({"error": "Provide either 'id' or 'mobile' parameter.", "credits": get_credits("api")}, status_code=400)
    else:
        if key_type == "id" and (not id or mobile): return JSONResponse({"error": "This Key is restricted to 'id' only.", "credits": get_credits("api")}, status_code=400)
        if key_type == "mobile" and (not mobile or id): return JSONResponse({"error": "This Key is restricted to 'mobile' only.", "credits": get_credits("api")}, status_code=400)

    cursor = db_conn.cursor()
    # Naye Columns: id, mobile, name, fname, address, alt, circle, email
    query = "SELECT id, mobile, name, fname, address, alt, circle, email FROM users WHERE 1=1"
    params = []
    search_term_for_log = "Unknown"

    if id: query += " AND id = ?"; params.append(str(id).strip()); search_term_for_log = id.strip()
    if mobile: query += " AND mobile = ?"; params.append(str(mobile).strip()); search_term_for_log = mobile.strip()

    query += " LIMIT 10" 
    start_time = time.time()
    try:
        cursor.execute(query, params)
        desc = cursor.getdescription()
        results = [{col[0]: row[i] for i, col in enumerate(desc)} for row in cursor]
        search_time = round((time.time() - start_time) * 1000, 2)
        found = len(results) > 0
        status_str = "Found" if found else "Not Found"
    except Exception as e:
        c.close(); conn.close()
        return JSONResponse({"error": f"Database error: {str(e)}", "credits": get_credits("api")}, status_code=500)

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("UPDATE api_keys SET used_count = used_count + 1, last_request_time = %s WHERE api_key = %s", (now_str, key))
    c.execute("INSERT INTO search_logs (api_key, number, status, timestamp, ip_address) VALUES (%s, %s, %s, %s, %s)", (key, search_term_for_log, status_str, now_str, client_ip))
    conn.commit()
    
    if used_count + 1 >= int(max_limit * 0.9) and used_count < int(max_limit * 0.9):
        admin_chat = get_setting("admin_chat_id")
        c.execute("SELECT bot_token FROM bots WHERE is_active = 1 LIMIT 1")
        admin_bot = c.fetchone()
        if admin_chat and admin_bot:
            await send_tg_msg_async(admin_bot[0], admin_chat, f"⚠️ <b>API Limit Alert (90%)!</b>\n\nKey: <code>{key}</code>\nUsed: {used_count + 1}/{max_limit}")
        
    c.close(); conn.close()
    return JSONResponse({"status": "Success" if found else "Not Found", "time_taken_ms": search_time, "credits": get_credits("api"), "total_results": len(results), "data": results})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("SERVER_PORT", 8080)))
