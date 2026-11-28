import httpx
from datetime import datetime, timedelta

_callbacks = {}

def init_bot(get_admin_db, get_db_conn, get_setting, get_credits, send_tg_msg_async, get_remote_table):
    _callbacks['get_admin_db'] = get_admin_db
    _callbacks['get_db_conn'] = get_db_conn
    _callbacks['get_setting'] = get_setting
    _callbacks['get_credits'] = get_credits
    _callbacks['send_tg_msg_async'] = send_tg_msg_async
    _callbacks['get_remote_table'] = get_remote_table


async def send_tg_with_keyboard(bot_token, chat_id, text, reply_markup):
    if not bot_token or not chat_id: return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": reply_markup},
                timeout=60.0
            )
    except Exception as e:
        print(f"❌ TG SEND ERROR: {e}")


async def answer_callback(bot_token, callback_id):
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
                json={"callback_query_id": callback_id},
                timeout=10.0
            )
    except: pass


def get_buttons():
    return {
        "inline_keyboard": [[
            {"text": "📱 Mobile", "callback_data": "search_mobile"},
            {"text": "🆔 ID", "callback_data": "search_id"}
        ]]
    }


async def handle_webhook(bot_token, data):
    bot_token = bot_token.strip()
    get_admin_db = _callbacks['get_admin_db']
    get_db_conn = _callbacks['get_db_conn']
    get_setting = _callbacks['get_setting']
    get_credits = _callbacks['get_credits']
    send_msg = _callbacks['send_tg_msg_async']
    get_remote_table = _callbacks['get_remote_table']

    # Check bot is active
    conn = get_admin_db(); c = conn.cursor()
    c.execute("SELECT is_active FROM bots WHERE bot_token = %s", (bot_token,))
    bot_row = c.fetchone()
    if not bot_row or bot_row[0] == 0:
        c.close(); conn.close()
        return {"status": "bot disabled"}

    # ==================== BUTTON CLICK ====================
    if "callback_query" in data:
        callback = data["callback_query"]
        chat_id = str(callback["message"]["chat"]["id"])
        callback_id = callback["id"]
        cb_data = callback.get("data", "")

        await answer_callback(bot_token, callback_id)

        c.execute("SELECT api_key FROM bot_users WHERE telegram_id = %s AND bot_token = %s", (chat_id, bot_token))
        user_row = c.fetchone()
        if not user_row or not user_row[0]:
            await send_msg(bot_token, chat_id, "❌ Please login first. Send /start")
            c.close(); conn.close()
            return {"ok": True}

        if cb_data == "search_mobile":
            c.execute("UPDATE bot_users SET search_mode = 'mobile' WHERE telegram_id = %s AND bot_token = %s", (chat_id, bot_token))
            conn.commit()
            await send_msg(bot_token, chat_id, "📱 Send me a <b>Mobile Number</b> to search.")
        elif cb_data == "search_id":
            c.execute("UPDATE bot_users SET search_mode = 'id' WHERE telegram_id = %s AND bot_token = %s", (chat_id, bot_token))
            conn.commit()
            await send_msg(bot_token, chat_id, "🆔 Send me an <b>ID</b> to search.")

        c.close(); conn.close()
        return {"ok": True}

    # ==================== TEXT MESSAGE ====================
    if "message" not in data or "text" not in data["message"]:
        return {"ok": True}

    chat_id = str(data["message"]["chat"]["id"])
    text = data["message"]["text"].strip()
    tg_username = data["message"]["from"].get("username", "Unknown")

    c.execute("SELECT is_blocked, api_key, search_mode FROM bot_users WHERE telegram_id = %s AND bot_token = %s", (chat_id, bot_token))
    user_row = c.fetchone()

    if user_row and user_row[0] == 1:
        await send_msg(bot_token, chat_id, "🚫 You are blocked by Admin.")
        c.close(); conn.close()
        return {"ok": True}

    # /start
    if text.startswith("/start"):
        if user_row and user_row[1]:
            await send_tg_with_keyboard(bot_token, chat_id,
                f"Welcome back! Key: <code>{user_row[1]}</code>\n\nChoose search type below 👇",
                get_buttons())
        else:
            await send_msg(bot_token, chat_id, "Welcome! 🔐 Please send your <b>API Key</b> to login (e.g. HTK-XXXX).")
        c.close(); conn.close()
        return {"ok": True}

    # LOGIN FLOW
    if not user_row or not user_row[1]:
        c.execute("SELECT api_key, expiry_date, is_active, key_type FROM api_keys WHERE api_key = %s", (text,))
        api_row = c.fetchone()
        if api_row:
            if api_row[2] == 0 or datetime.strptime(api_row[1], "%Y-%m-%d").date() < datetime.now().date():
                await send_msg(bot_token, chat_id, "❌ This API Key is expired or blocked.")
            else:
                c.execute("""INSERT INTO bot_users (telegram_id, bot_token, api_key, username, joined_date, search_mode)
                             VALUES (%s, %s, %s, %s, %s, NULL)
                             ON CONFLICT (telegram_id, bot_token) DO UPDATE SET api_key = EXCLUDED.api_key, is_blocked = 0, search_mode = NULL""",
                          (chat_id, bot_token, text, tg_username, datetime.now().strftime("%Y-%m-%d")))
                conn.commit()
                await send_tg_with_keyboard(bot_token, chat_id,
                    f"✅ Login Successful!\nKey Type: <b>{api_row[3].upper()}</b>\n\nChoose search type 👇",
                    get_buttons())
        else:
            await send_msg(bot_token, chat_id, "❌ Invalid API Key.")
        c.close(); conn.close()
        return {"ok": True}

    # SEARCH FLOW
    api_key = user_row[1]
    search_mode = user_row[2]

    if not search_mode:
        await send_tg_with_keyboard(bot_token, chat_id,
            "⚠️ Pehle search type select karo 👇",
            get_buttons())
        c.close(); conn.close()
        return {"ok": True}

    c.execute("SELECT expiry_date, max_limit, used_count, is_active, rate_limit_seconds, last_request_time, key_type FROM api_keys WHERE api_key = %s", (api_key,))
    api_data = c.fetchone()

    if not api_data or api_data[3] == 0 or datetime.strptime(api_data[0], "%Y-%m-%d").date() < datetime.now().date():
        await send_msg(bot_token, chat_id, "❌ Your API Key is expired or blocked.")
        c.close(); conn.close()
        return {"ok": True}

    expiry, max_limit, used_count, is_active, rate_limit, last_req_str, key_type = api_data

    # Key type validation
    if key_type == "mobile" and search_mode != "mobile":
        await send_msg(bot_token, chat_id, "❌ Your API Key is restricted to Mobile search only.")
        c.close(); conn.close()
        return {"ok": True}
    if key_type == "id" and search_mode != "id":
        await send_msg(bot_token, chat_id, "❌ Your API Key is restricted to ID search only.")
        c.close(); conn.close()
        return {"ok": True}

    if used_count >= max_limit:
        await send_msg(bot_token, chat_id, "❌ API Limit Exceeded. Contact Admin.")
        c.close(); conn.close()
        return {"ok": True}

    if last_req_str:
        last_req_time = datetime.strptime(last_req_str, "%Y-%m-%d %H:%M:%S")
        if (datetime.now() - last_req_time).total_seconds() < rate_limit:
            await send_msg(bot_token, chat_id, f"⏳ Rate limit exceeded. Wait {rate_limit}s.")
            c.close(); conn.close()
            return {"ok": True}

    # SEARCH IN REMOTE DB
    remote_table = get_remote_table()
    db_conn = get_db_conn()
    cursor = db_conn.cursor()
    query = f"SELECT id, mobile, name, fname, address, alt, circle, email FROM {remote_table} WHERE {search_mode} = ? LIMIT 5"

    try:
        cursor.execute(query, (text,))
        desc = cursor.getdescription()
        results = [{col[0]: r[i] for i, col in enumerate(desc)} for r in cursor]
    except Exception as e:
        await send_msg(bot_token, chat_id, f"❌ Database Error: {e}")
        c.close(); conn.close()
        return {"ok": True}

    found = len(results) > 0
    status_str = "Found" if found else "Not Found"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("UPDATE api_keys SET used_count = used_count + 1, last_request_time = %s WHERE api_key = %s", (now_str, api_key))
    c.execute("INSERT INTO search_logs (api_key, number, status, timestamp, ip_address) VALUES (%s, %s, %s, %s, %s)", (api_key, text, status_str, now_str, f"TG-{chat_id}"))
    c.execute("UPDATE bot_users SET search_mode = NULL WHERE telegram_id = %s AND bot_token = %s", (chat_id, bot_token))
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

    await send_tg_with_keyboard(bot_token, chat_id, reply, get_buttons())

    c.close(); conn.close()
    return {"ok": True}
