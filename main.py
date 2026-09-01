import os
import json
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from openai import OpenAI
from supabase import create_client, Client

load_dotenv()

app = FastAPI()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------------------------------------------------------------------
# ATUPCY BRIDGE DATABASE HELPERS
# ---------------------------------------------------------------------

BRIDGE_BUSINESSES_TABLE = "atupcy_bridge_businesses"
BRIDGE_CUSTOMERS_TABLE = "atupcy_bridge_customers"
BRIDGE_CONVERSATIONS_TABLE = "atupcy_bridge_conversations"
BRIDGE_MESSAGES_TABLE = "atupcy_bridge_messages"
BRIDGE_USAGE_TABLE = "atupcy_bridge_usage_events"


def get_bridge_business():
    """Get the active Atupcy Bridge business."""
    response = (
        supabase
        .table(BRIDGE_BUSINESSES_TABLE)
        .select("*")
        .eq("status", "active")
        .limit(1)
        .execute()
    )

    rows = response.data or []

    return rows[0] if rows else None

# The business owner's language — what customer messages get translated
# into. Kept simple for MVP; could become configurable per business later.
OWNER_LANGUAGE = "English"

@app.get("/")
async def health_check():
    return {"status": "ok", "message": "Business relay bot is running"}

@app.get("/bridge-test")
async def bridge_test():
    business = get_bridge_business()

    if not business:
        return {
            "status": "error",
            "message": "No active Atupcy Bridge business found"
        }

    return {
        "status": "ok",
        "business": business
    }

async def get_or_create_bridge_customer(
    business_id: str,
    customer_chat_id: int,
    customer_name: str | None = None
):
    """
    Find an existing Atupcy Bridge customer or create a new one.
    """

    response = (
        supabase
        .table("atupcy_bridge_customers")
        .select("*")
        .eq("business_id", business_id)
        .eq("channel", "telegram")
        .eq("channel_user_id", str(customer_chat_id))
        .execute()
    )

    customers = response.data or []

    if customers:
        return customers[0]

    new_customer = {
        "business_id": business_id,
        "channel": "telegram",
        "channel_user_id": str(customer_chat_id),
        "name": customer_name,
    }

    response = (
        supabase
        .table("atupcy_bridge_customers")
        .insert(new_customer)
        .execute()
    )

    return response.data[0]

@app.get("/telegram-webhook-info")
async def telegram_webhook_info():
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{TELEGRAM_API_URL}/getWebhookInfo"
        )

    return response.json()

@app.post("/webhook")
async def webhook(request: Request):
    update = await request.json()
    message = update.get("message")
    if not message:
        return {"ok": True}

    chat_id = message["chat"]["id"]
    customer_name = message["chat"].get("first_name")
    text = message.get("text")
    voice = message.get("voice")

    if text and text.startswith("/register"):
        await handle_register(chat_id)
        return {"ok": True}
    if text and text.startswith("/switch"):
        await handle_switch(chat_id, text)
        return {"ok": True}
    if text and text.startswith("/customers"):
        await handle_list_customers(chat_id)
        return {"ok": True}

    if not text and not voice:
        return {"ok": True}

    bridge_business = get_bridge_business()

    if bridge_business:
        bridge_customer = await get_or_create_bridge_customer(
            business_id=bridge_business["id"],
            customer_chat_id=chat_id,
            customer_name=customer_name
        )

        print("BRIDGE CUSTOMER:", bridge_customer)

        return {"ok": True}
    
    owner_id = get_registered_owner()

    if owner_id is None:
        await send_message(chat_id, "No business owner has registered yet. Send /register if this is your business account.")
        return {"ok": True}

    try:
        if voice:
            text = await transcribe_voice(voice["file_id"])
            if not text or not text.strip():
                await send_message(chat_id, "I couldn't make out any speech in that voice note — try again?")
                return {"ok": True}

        if chat_id == owner_id:
            await handle_owner_message(chat_id, text)
        else:
            await handle_customer_message(chat_id, text)
    except Exception as e:
        await send_message(chat_id, f"Sorry, something went wrong: {e}")

    return {"ok": True}

def get_registered_owner() -> int | None:
    """Return the registered business owner's chat_id, or None if unset."""
    response = supabase.table("relay_config").select("owner_chat_id").eq("id", 1).execute()
    rows = response.data or []
    return rows[0]["owner_chat_id"] if rows else None

async def handle_register(chat_id: int):
    """
    Register this chat as the business owner. Simple MVP rule: whoever
    registers first becomes the owner. Re-registering isn't supported
    yet (would need an "are you sure" flow to avoid accidental takeover).
    """
    existing_owner = get_registered_owner()
    if existing_owner is not None:
        if existing_owner == chat_id:
            await send_message(chat_id, "You're already registered as the business owner.")
        else:
            await send_message(chat_id, "A business owner is already registered for this bot.")
        return

    supabase.table("relay_config").upsert({"id": 1, "owner_chat_id": chat_id}).execute()
    await send_message(
        chat_id,
        "✅ You're now registered as the business owner. Customers who message this bot will have their messages "
        "translated to you automatically. Use /customers to see who's messaged you, and /switch <number> to reply to a specific customer.",
    )

async def get_or_create_customer(owner_id: int, customer_chat_id: int) -> int:
    """
    Look up this customer's short number (e.g. 1, 2, 3) for this owner,
    creating a new entry with the next number if they're new.
    """
    response = (
        supabase.table("relay_customers")
        .select("customer_number")
        .eq("owner_chat_id", owner_id)
        .eq("customer_chat_id", customer_chat_id)
        .execute()
    )
    rows = response.data or []
    if rows:
        return rows[0]["customer_number"]

    # New customer — assign the next available number for this owner.
    count_response = (
        supabase.table("relay_customers")
        .select("customer_number")
        .eq("owner_chat_id", owner_id)
        .execute()
    )
    existing_numbers = [r["customer_number"] for r in (count_response.data or [])]
    next_number = max(existing_numbers, default=0) + 1

    supabase.table("relay_customers").insert(
        {"owner_chat_id": owner_id, "customer_chat_id": customer_chat_id, "customer_number": next_number}
    ).execute()
    return next_number

async def handle_list_customers(chat_id: int):
    owner_id = get_registered_owner()
    if owner_id != chat_id:
        await send_message(chat_id, "Only the registered business owner can view the customer list.")
        return

    response = (
        supabase.table("relay_customers")
        .select("customer_number")
        .eq("owner_chat_id", owner_id)
        .order("customer_number")
        .execute()
    )
    rows = response.data or []
    if not rows:
        await send_message(chat_id, "No customers have messaged yet.")
        return

    numbers = ", ".join(f"Customer {r['customer_number']}" for r in rows)
    await send_message(chat_id, f"Known customers: {numbers}\n\nUse /switch <number> to reply to one.")

async def handle_switch(chat_id: int, text: str):
    owner_id = get_registered_owner()
    if owner_id != chat_id:
        await send_message(chat_id, "Only the registered business owner can switch active customers.")
        return

    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await send_message(chat_id, "Usage: /switch <customer number>\nExample: /switch 2\n\nUse /customers to see the list.")
        return

    customer_number = int(parts[1].strip())
    response = (
        supabase.table("relay_customers")
        .select("customer_chat_id")
        .eq("owner_chat_id", owner_id)
        .eq("customer_number", customer_number)
        .execute()
    )
    rows = response.data or []
    if not rows:
        await send_message(chat_id, f"No customer numbered {customer_number} found. Use /customers to see the list.")
        return

    supabase.table("relay_config").update({"active_customer_chat_id": rows[0]["customer_chat_id"]}).eq("id", 1).execute()
    await send_message(chat_id, f"✅ Now replying to Customer {customer_number}.")

async def handle_customer_message(customer_chat_id: int, text: str):
    """A customer messaged — translate to the owner's language and forward it."""
    owner_id = get_registered_owner()
    customer_number = await get_or_create_customer(owner_id, customer_chat_id)

    result = translate(text, target_language=OWNER_LANGUAGE)

    # Save the detected language so the owner's reply can be translated
    # back into it later (handle_owner_message looks this up).
    supabase.table("relay_messages").insert(
        {"customer_chat_id": customer_chat_id, "customer_language": result["source_language"]}
    ).execute()

    await send_message(
        owner_id,
        f"📩 Customer {customer_number} ({result['source_language']}):\n\n{result['translated_text']}\n\n"
        f"Reply with /switch {customer_number} if you're not already talking to them.",
    )

    if result["source_language"].lower() == "english":
        ack_text = "Thanks for your message — we'll get back to you shortly."
    else: 
        ack_result = translate(
            "Thanks for your message — we'll get back to you shortly.",
            target_language=result["source_language"],
        )
        ack_text = ack_result["translated_text"]
    await send_message(customer_chat_id, ack_text)

async def handle_owner_message(owner_id: int, text: str):
    """The owner sent a message — translate it to the active customer's language and send it."""
    config_response = supabase.table("relay_config").select("active_customer_chat_id").eq("id", 1).execute()
    rows = config_response.data or []
    active_customer_id = rows[0].get("active_customer_chat_id") if rows else None

    if not active_customer_id:
        await send_message(owner_id, "No active customer set. Use /switch <number> first (see /customers for the list).")
        return

    # Look up the customer's most recent detected language, so replies
    # go back in the language they've been writing in.
    history_response = (
        supabase.table("relay_messages")
        .select("customer_language")
        .eq("customer_chat_id", active_customer_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    history_rows = history_response.data or []
    customer_language = history_rows[0]["customer_language"] if history_rows else "English"

    result = translate(text, target_language=customer_language)
    await send_message(active_customer_id, result["translated_text"])
    await send_message(owner_id, f"✅ Sent to customer in {customer_language}.")

def translate(text: str, target_language: str) -> dict:
    """
    Detect the source language and translate into target_language
    specifically (unlike the personal bot, relay mode always has an
    explicit target — the other party in the conversation — rather
    than inferring a default).
    """
    system_prompt = f"""You are a translation engine. Given a message:
1. Detect the source language.
2. Translate it accurately into {target_language}, preserving tone and meaning.

Respond with ONLY a JSON object in this exact format:
{{"source_language": "...", "translated_text": "..."}}
"""

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content.strip()
    return json.loads(raw)

async def transcribe_voice(file_id: str) -> str:
    async with httpx.AsyncClient() as client:
        file_info_resp = await client.get(f"{TELEGRAM_API_URL}/getFile", params={"file_id": file_id})
        file_path = file_info_resp.json()["result"]["file_path"]
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        audio_resp = await client.get(file_url)
        audio_bytes = audio_resp.content

    if not audio_bytes:
        raise ValueError("Downloaded voice file was empty")

    transcription = openai_client.audio.transcriptions.create(
        model="whisper-1",
        file=("voice.ogg", audio_bytes, "audio/ogg"),
    )
    return transcription.text

async def send_message(chat_id: int, text: str):
    async with httpx.AsyncClient() as client:
        await client.post(f"{TELEGRAM_API_URL}/sendMessage", json={"chat_id": chat_id, "text": text})