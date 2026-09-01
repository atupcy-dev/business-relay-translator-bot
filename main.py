import os
import json
import httpx
from datetime import datetime, timezone
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


BRIDGE_BUSINESSES_TABLE = "atupcy_bridge_businesses"
BRIDGE_CUSTOMERS_TABLE = "atupcy_bridge_customers"
BRIDGE_CONVERSATIONS_TABLE = "atupcy_bridge_conversations"
BRIDGE_MESSAGES_TABLE = "atupcy_bridge_messages"

DEFAULT_OWNER_LANGUAGE = "English"


@app.get("/")
async def health_check():
    return {
        "status": "ok",
        "message": "Atupcy Bridge is running"
    }


@app.get("/telegram-webhook-info")
async def telegram_webhook_info():
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{TELEGRAM_API_URL}/getWebhookInfo"
        )

    return response.json()


@app.get("/bridge-test")
async def bridge_test():
    response = (
        supabase
        .table(BRIDGE_BUSINESSES_TABLE)
        .select("id, business_name, owner_chat_id, owner_language, default_channel, status")
        .execute()
    )

    return {
            "status": "ok",
            "supabase_rows": response.data,
            "row_count": len(response.data or [])
        }

def get_active_business():
    """
    Get the active Atupcy Bridge business.

    MVP:
    At the moment one Telegram bot is connected to one active business.
    """

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


async def get_or_create_bridge_customer(
    business_id: str,
    customer_chat_id: int,
    customer_name: str | None = None
):
    """
    Find an existing Telegram customer for this business,
    or create a new one.
    """

    response = (
        supabase
        .table(BRIDGE_CUSTOMERS_TABLE)
        .select("*")
        .eq("business_id", business_id)
        .eq("channel", "telegram")
        .eq("channel_user_id", str(customer_chat_id))
        .limit(1)
        .execute()
    )

    customers = response.data or []

    if customers:
        return customers[0]

    new_customer = {
        "business_id": business_id,
        "channel": "telegram",
        "channel_user_id": str(customer_chat_id),
        "name": customer_name
    }

    response = (
        supabase
        .table(BRIDGE_CUSTOMERS_TABLE)
        .insert(new_customer)
        .execute()
    )

    rows = response.data or []

    if not rows:
        raise Exception("Failed to create Bridge customer")

    return rows[0]


def get_or_create_conversation(
    business_id: str,
    customer_id: str
):
    """
    Find the customer's active conversation.

    If none exists, create one.
    """

    response = (
        supabase
        .table(BRIDGE_CONVERSATIONS_TABLE)
        .select("*")
        .eq("business_id", business_id)
        .eq("customer_id", customer_id)
        .eq("channel", "telegram")
        .eq("status", "active")
        .order("last_message_at", desc=True)
        .limit(1)
        .execute()
    )

    conversations = response.data or []

    if conversations:
        return conversations[0]

    new_conversation = {
        "business_id": business_id,
        "customer_id": customer_id,
        "status": "active",
        "channel": "telegram"
    }

    response = (
        supabase
        .table(BRIDGE_CONVERSATIONS_TABLE)
        .insert(new_conversation)
        .execute()
    )

    rows = response.data or []

    if not rows:
        raise Exception("Failed to create Bridge conversation")

    return rows[0]


def get_owner_active_conversation(business_id: str):
    """
    Find the most recently active conversation for the business.

    This replaces the old relay_config.active_customer_chat_id
    routing system.
    """

    response = (
        supabase
        .table(BRIDGE_CONVERSATIONS_TABLE)
        .select("*")
        .eq("business_id", business_id)
        .eq("channel", "telegram")
        .eq("status", "active")
        .order("last_message_at", desc=True)
        .limit(1)
        .execute()
    )

    rows = response.data or []

    return rows[0] if rows else None


def get_customer_by_id(customer_id: str):
    """
    Get a Bridge customer by database ID.
    """

    response = (
        supabase
        .table(BRIDGE_CUSTOMERS_TABLE)
        .select("*")
        .eq("id", customer_id)
        .limit(1)
        .execute()
    )

    rows = response.data or []

    return rows[0] if rows else None


def get_latest_customer_language(conversation_id: str):
    """
    Get the most recently detected customer language
    from this conversation.
    """

    response = (
        supabase
        .table(BRIDGE_MESSAGES_TABLE)
        .select("language")
        .eq("conversation_id", conversation_id)
        .eq("sender_type", "customer")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    rows = response.data or []

    if rows and rows[0].get("language"):
        return rows[0]["language"]

    return DEFAULT_OWNER_LANGUAGE

def save_bridge_message(
    conversation_id: str,
    sender_type: str,
    original_text: str,
    translated_text: str,
    language: str,
    response_source: str = "ai"
):
    """
    Save a message to the Atupcy Bridge message table.
    """

    supabase.table(BRIDGE_MESSAGES_TABLE).insert(
        {
            "conversation_id": conversation_id,
            "sender_type": sender_type,
            "original_text": original_text,
            "translated_text": translated_text,
            "language": language,
            "response_source": response_source
        }
    ).execute()


def update_conversation_timestamp(conversation_id: str):
    """
    Update the conversation's last_message_at timestamp.
    """

    timestamp = datetime.now(timezone.utc).isoformat()

    supabase.table(BRIDGE_CONVERSATIONS_TABLE).update(
        {
            "last_message_at": timestamp
        }
    ).eq("id", conversation_id).execute()


@app.post("/webhook")
async def webhook(request: Request):

    update = await request.json()

    print("TELEGRAM UPDATE:", update)

    message = update.get("message")

    if not message:
        return {"ok": True}

    chat = message.get("chat") or {}

    chat_id = chat.get("id")

    if not chat_id:
        return {"ok": True}

    customer_name = (
        chat.get("first_name")
        or chat.get("username")
        or "Telegram User"
    )

    text = message.get("text")
    voice = message.get("voice")

    if not text and not voice:
        return {"ok": True}


    business = get_active_business()

    if not business:
        await send_message(
            chat_id,
            "Atupcy Bridge is not currently connected to an active business."
        )
        return {"ok": True}

    owner_chat_id = business.get("owner_chat_id")

    if not owner_chat_id:
        print("ERROR: Active business has no owner_chat_id")

        await send_message(
            chat_id,
            "Atupcy Bridge is not fully configured yet."
        )

        return {"ok": True}

    owner_chat_id = int(owner_chat_id)


    if chat_id == owner_chat_id:
        try:
            await handle_owner_message(
                owner_chat_id=owner_chat_id,
                business=business,
                text=text,
                voice=voice
            )

        except Exception as e:
            print("OWNER MESSAGE ERROR:", repr(e))

            await send_message(
                owner_chat_id,
                "Sorry, something went wrong while processing your message."
            )

        return {"ok": True}


    try:
        await handle_customer_message(
            customer_chat_id=chat_id,
            customer_name=customer_name,
            business=business,
            text=text,
            voice=voice
        )

    except Exception as e:
        print("CUSTOMER MESSAGE ERROR:", repr(e))

        await send_message(
            chat_id,
            "Sorry, something went wrong while processing your message."
        )

    return {"ok": True}


async def handle_customer_message(
    customer_chat_id: int,
    customer_name: str,
    business: dict,
    text: str | None,
    voice: dict | None
):

    business_id = business["id"]
    owner_chat_id = int(business["owner_chat_id"])

    owner_language = (
        business.get("owner_language")
        or DEFAULT_OWNER_LANGUAGE
    )


    if voice:
        text = await transcribe_voice(voice["file_id"])

        if not text or not text.strip():
            await send_message(
                customer_chat_id,
                "I couldn't make out any speech in that voice note. Please try again."
            )
            return

    if not text or not text.strip():
        return

    customer = await get_or_create_bridge_customer(
        business_id=business_id,
        customer_chat_id=customer_chat_id,
        customer_name=customer_name
    )

    customer_id = customer["id"]

    print("ATUPCY BRIDGE CUSTOMER:", customer)


    conversation = get_or_create_conversation(
        business_id=business_id,
        customer_id=customer_id
    )

    conversation_id = conversation["id"]

    print("ATUPCY BRIDGE CONVERSATION:", conversation)


    result = translate(
        text=text,
        target_language=owner_language
    )

    source_language = result["source_language"]
    translated_text = result["translated_text"]


    save_bridge_message(
        conversation_id=conversation_id,
        sender_type="customer",
        original_text=text,
        translated_text=translated_text,
        language=source_language,
        response_source="ai"
    )

    update_conversation_timestamp(conversation_id)


    customer_display_name = customer.get("name") or "Customer"

    owner_message = (
        f"📩 New customer message\n\n"
        f"Customer: {customer_display_name}\n"
        f"Language: {source_language}\n\n"
        f"{translated_text}"
    )

    await send_message(
        owner_chat_id,
        owner_message
    )

    if source_language.lower() == "english":

        acknowledgement = (
            "Thanks for your message. We'll get back to you shortly."
        )

    else:

        acknowledgement_result = translate(
            text="Thanks for your message. We'll get back to you shortly.",
            target_language=source_language
        )

        acknowledgement = acknowledgement_result["translated_text"]

    await send_message(
        customer_chat_id,
        acknowledgement
    )


async def handle_owner_message(
    owner_chat_id: int,
    business: dict,
    text: str | None,
    voice: dict | None
):

    business_id = business["id"]


    if voice:
        text = await transcribe_voice(voice["file_id"])

        if not text or not text.strip():
            await send_message(
                owner_chat_id,
                "I couldn't make out that voice note. Please try again."
            )
            return

    if not text or not text.strip():
        return


    conversation = get_owner_active_conversation(
        business_id=business_id
    )

    if not conversation:
        await send_message(
            owner_chat_id,
            "There is no active customer conversation yet."
        )
        return

    conversation_id = conversation["id"]
    customer_id = conversation["customer_id"]


    customer = get_customer_by_id(customer_id)

    if not customer:
        await send_message(
            owner_chat_id,
            "The customer for this conversation could not be found."
        )
        return

    customer_chat_id = int(customer["channel_user_id"])


    customer_language = get_latest_customer_language(
        conversation_id
    )


    result = translate(
        text=text,
        target_language=customer_language
    )

    translated_text = result["translated_text"]


    save_bridge_message(
        conversation_id=conversation_id,
        sender_type="owner",
        original_text=text,
        translated_text=translated_text,
        language=customer_language,
        response_source="ai"
    )

    update_conversation_timestamp(conversation_id)


    await send_message(
        customer_chat_id,
        translated_text
    )

    await send_message(
        owner_chat_id,
        f"✅ Message sent to {customer.get('name') or 'customer'} in {customer_language}."
    )


def translate(
    text: str,
    target_language: str
) -> dict:

    system_prompt = f"""
You are the translation engine for Atupcy Bridge.

Given a message:

1. Detect the source language.
2. Translate the message accurately into {target_language}.
3. Preserve the original meaning, tone, intent, and context.
4. Do not add explanations.
5. Do not answer the message.
6. Only translate it.

Respond with ONLY a JSON object in exactly this format:

{{
    "source_language": "...",
    "translated_text": "..."
}}
"""

    response = openai_client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": text
            }
        ],
        temperature=0.2,
        response_format={
            "type": "json_object"
        }
    )

    raw = response.choices[0].message.content.strip()

    return json.loads(raw)


async def transcribe_voice(file_id: str) -> str:

    async with httpx.AsyncClient() as client:

        file_info_response = await client.get(
            f"{TELEGRAM_API_URL}/getFile",
            params={
                "file_id": file_id
            }
        )

        file_info_response.raise_for_status()

        file_data = file_info_response.json()

        file_path = file_data["result"]["file_path"]

        file_url = (
            f"https://api.telegram.org/file/bot"
            f"{BOT_TOKEN}/{file_path}"
        )

        audio_response = await client.get(file_url)

        audio_response.raise_for_status()

        audio_bytes = audio_response.content

    if not audio_bytes:
        raise ValueError(
            "Downloaded voice file was empty"
        )

    transcription = openai_client.audio.transcriptions.create(
        model="whisper-1",
        file=(
            "voice.ogg",
            audio_bytes,
            "audio/ogg"
        )
    )

    return transcription.text


async def send_message(
    chat_id: int,
    text: str
):

    async with httpx.AsyncClient() as client:

        response = await client.post(
            f"{TELEGRAM_API_URL}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text
            }
        )

        response.raise_for_status()