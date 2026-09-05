import os
import json
import traceback
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

AI_SUPPORT_WEBHOOK_URL = os.getenv("AI_SUPPORT_WEBHOOK_URL")


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
    business = get_active_business()

    if not business:
        return {
            "status": "error",
            "message": "No active Atupcy Bridge business found"
        }

    return {
        "status": "ok",
        "business": business
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
        .eq("telegram_chat_id", str(customer_chat_id))
        .limit(1)
        .execute()
    )

    customers = response.data or []

    if customers:
        return customers[0]

    # Get existing customer numbers for this business
    number_response = (
        supabase
        .table(BRIDGE_CUSTOMERS_TABLE)
        .select("customer_number")
        .eq("business_id", business_id)
        .execute()
    )

    existing_numbers = [
        int(row["customer_number"])
        for row in (number_response.data or [])
        if row.get("customer_number") is not None
    ]

    next_customer_number = max(existing_numbers, default=0) + 1

    new_customer = {
        "business_id": business_id,
        "telegram_chat_id": str(customer_chat_id),
        "customer_number": next_customer_number,
        "name": customer_name,
    }

    response = (
        supabase
        .table(BRIDGE_CUSTOMERS_TABLE)
        .insert(new_customer)
        .execute()
    )

    rows = response.data or []

    if not rows:
        raise Exception("Failed to create Atupcy Bridge customer")

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

def get_conversation_messages(conversation_id: str):
    response = (
        supabase
        .table(BRIDGE_MESSAGES_TABLE)
        .select("*")
        .eq("conversation_id", conversation_id)
        .order("created_at", desc=False)
        .execute()
    )

    return response.data or []

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

        
        if text and text.strip().lower() == "/customers":

            try:
                await handle_customers_command(
                    owner_chat_id=owner_chat_id,
                    business=business
                )

            except Exception as e:
                print(
                    "CUSTOMERS COMMAND ERROR:",
                    repr(e)
                )

                await send_message(
                    owner_chat_id,
                    "Sorry, something went wrong while loading your customers."
                )

            return {"ok": True}


        try:
            await handle_owner_message(
                owner_chat_id=owner_chat_id,
                business=business,
                text=text,
                voice=voice
            )

        except Exception as e:
            print(
                "OWNER MESSAGE ERROR:",
                repr(e)
            )

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
        print(
            "CUSTOMER MESSAGE ERROR:",
            repr(e)
        )

        traceback.print_exc()

        await send_message(
            chat_id,
            "Sorry, something went wrong while processing your message."
        )

    return {"ok": True}

@app.post("/support-test")
async def support_test():
    result = await send_to_ai_support(
        conversation_id="test-conversation",
        message="Hello, can you help me with my order?"
    )

    return {
        "status": "ok",
        "result": result
    }

async def handle_customer_message(
    customer_chat_id: int,
    customer_name: str,
    business: dict,
    text: str | None,
    voice: dict | None
):

    business_id = business["id"]
    owner_chat_id = int(business["owner_chat_id"])

    was_voice = voice is not None

    owner_language = (
        business.get("owner_language")
        or DEFAULT_OWNER_LANGUAGE
    )

    customer = await get_or_create_bridge_customer(
        business_id=business_id,
        customer_chat_id=customer_chat_id,
        customer_name=customer_name
    )

    customer_id = customer["id"]

    print(
        "ATUPCY BRIDGE CUSTOMER:",
        customer
    )

    conversation = get_or_create_conversation(
        business_id=business_id,
        customer_id=customer_id
    )

    conversation_id = conversation["id"]

    print(
        "ATUPCY BRIDGE CONVERSATION:",
        conversation
    )


    if not text and not voice:
        return

    
    required_credits = 7 if voice else 4

    try:

        credit_check = check_bridge_credits(
            business_id=business_id,
            credits=required_credits
        )

        if not credit_check.get(
            "has_enough_credits",
            False
        ):

            await send_message(
                customer_chat_id,
                "Atupcy Bridge has reached its available usage limit. Please contact Atupcy LTD to continue."
            )

            return

    except Exception as e:

        print(
            "CUSTOMER CREDIT CHECK FAILED:",
            repr(e)
        )

        await send_message(
            customer_chat_id,
            "I'm sorry, but I can't process your message right now. Please try again later."
        )

        return


    if voice:

        text = await transcribe_voice(
            voice["file_id"]
        )

        if not text or not text.strip():

            await send_message(
                customer_chat_id,
                "I couldn't make out any speech in that voice note. Please try again."
            )

            return

        try:

            consume_bridge_credits(
                business_id=business_id,
                credits=3,
                conversation_id=conversation_id,
                event_type="voice_transcription",
                channel="telegram",
                description="Voice message transcription"
            )

        except Exception as e:

            print(
                "VOICE CREDIT CONSUMPTION FAILED:",
                repr(e)
            )

            return


    if not text or not text.strip():
        return


    try:

        consume_bridge_credits(
            business_id=business_id,
            credits=1,
            conversation_id=conversation_id,
            event_type="translation",
            channel="telegram",
            description="Customer message translation"
        )

    except Exception as e:

        print(
            "CUSTOMER TRANSLATION CREDIT CONSUMPTION FAILED:",
            repr(e)
        )

        await send_message(
            customer_chat_id,
            "I'm sorry, but I can't process your message right now. Please try again later."
        )

        return

    result = translate(
        text=text,
        target_language=owner_language
    )

    source_language = result["source_language"]
    translated_text = result["translated_text"]


    if was_voice:

        save_usage_event(
            business_id=business_id,
            conversation_id=conversation_id,
            event_type="voice_transcription",
            channel="telegram",
            language=source_language
        )

    save_usage_event(
        business_id=business_id,
        conversation_id=conversation_id,
        event_type="translation",
        channel="telegram",
        language=source_language
    )

    save_usage_event(
        business_id=business_id,
        conversation_id=conversation_id,
        event_type="customer_message",
        channel="telegram",
        language=source_language
    )


    supabase.table(
        BRIDGE_CUSTOMERS_TABLE
    ).update(
        {
            "language": source_language,
            "last_seen_at": datetime.now(
                timezone.utc
            ).isoformat()
        }
    ).eq(
        "id",
        customer_id
    ).execute()


    save_bridge_message(
        conversation_id=conversation_id,
        sender_type="customer",
        original_text=text,
        translated_text=translated_text,
        language=source_language,
        response_source="ai"
    )

    update_conversation_timestamp(
        conversation_id
    )

    customer_display_name = (
        customer.get("name")
        or "Customer"
    )


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


    try:

        consume_bridge_credits(
            business_id=business_id,
            credits=2,
            conversation_id=conversation_id,
            event_type="ai_support",
            channel="telegram",
            description="AI customer support"
        )

    except Exception as e:

        print(
            "AI SUPPORT CREDIT CONSUMPTION FAILED:",
            repr(e)
        )

        await send_message(
            customer_chat_id,
            "I'm sorry, but I'm unable to process your request right now. Please try again later."
        )

        return

    support_result = await send_to_ai_support(
        conversation_id=conversation_id,
        message=text
    )

    ai_reply = support_result.get("reply")

    escalated = bool(
        support_result.get(
            "escalated",
            False
        )
    )

    save_usage_event(
        business_id=business_id,
        conversation_id=conversation_id,
        event_type="ai_support",
        channel="telegram",
        language=source_language
    )


    if ai_reply and ai_reply.strip():

        try:

            consume_bridge_credits(
                business_id=business_id,
                credits=1,
                conversation_id=conversation_id,
                event_type="translation",
                channel="telegram",
                description="AI response translation"
            )

        except Exception as e:

            print(
                "AI RESPONSE TRANSLATION CREDIT CONSUMPTION FAILED:",
                repr(e)
            )

            await send_message(
                customer_chat_id,
                "I'm sorry, but I couldn't complete the response right now. Please try again later."
            )

            return

        ai_translation_result = translate(
            text=ai_reply,
            target_language=source_language
        )

        translated_ai_reply = (
            ai_translation_result["translated_text"]
        )

        save_bridge_message(
            conversation_id=conversation_id,
            sender_type="owner",
            original_text=ai_reply,
            translated_text=translated_ai_reply,
            language=source_language,
            response_source="ai"
        )

        await send_message(
            customer_chat_id,
            translated_ai_reply
        )


    if escalated:

        await send_message(
            owner_chat_id,
            f"🚨 AI Support Escalation\n\n"
            f"Customer: {customer_display_name}\n"
            f"Language: {source_language}\n\n"
            f"The AI support agent has flagged this conversation for human review."
        )

def get_conversation_by_id(conversation_id: str):
    """
    Get one Atupcy Bridge conversation by database ID.
    """

    response = (
        supabase
        .table(BRIDGE_CONVERSATIONS_TABLE)
        .select("*")
        .eq("id", conversation_id)
        .limit(1)
        .execute()
    )

    rows = response.data or []

    return rows[0] if rows else None

def get_business_conversations(business_id: str):
    """
    Get all Telegram conversations for a business,
    with the most recently active conversations first.
    """

    response = (
        supabase
        .table(BRIDGE_CONVERSATIONS_TABLE)
        .select("*")
        .eq("business_id", business_id)
        .eq("channel", "telegram")
        .order("last_message_at", desc=True)
        .execute()
    )

    return response.data or []

def close_conversation(conversation_id: str):
    """
    Close an active Atupcy Bridge conversation.
    """

    supabase.table(BRIDGE_CONVERSATIONS_TABLE).update(
        {
            "status": "closed"
        }
    ).eq(
        "id",
        conversation_id
    ).execute()

async def handle_owner_message(
    owner_chat_id: int,
    business: dict,
    text: str | None,
    voice: dict | None
):

    business_id = business["id"]
    was_voice = voice is not None

    
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


    customer = get_customer_by_id(
        customer_id
    )

    if not customer:

        await send_message(
            owner_chat_id,
            "The customer for this conversation could not be found."
        )

        return

    customer_chat_id = int(
        customer["telegram_chat_id"]
    )

    customer_language = get_latest_customer_language(
        conversation_id
    )


    if not text and not voice:
        return


    required_credits = 4 if voice else 1

    try:

        credit_check = check_bridge_credits(
            business_id=business_id,
            credits=required_credits
        )

        if not credit_check.get(
            "has_enough_credits",
            False
        ):

            await send_message(
                owner_chat_id,
                "Your Atupcy Bridge usage limit has been reached. Please contact Atupcy LTD to continue."
            )

            return

    except Exception as e:

        print(
            "OWNER CREDIT CHECK FAILED:",
            repr(e)
        )

        await send_message(
            owner_chat_id,
            "I'm sorry, but I can't process this message right now. Please try again later."
        )

        return


    if voice:

        text = await transcribe_voice(
            voice["file_id"]
        )

        if not text or not text.strip():

            await send_message(
                owner_chat_id,
                "I couldn't make out that voice note. Please try again."
            )

            return

        try:

            consume_bridge_credits(
                business_id=business_id,
                credits=3,
                conversation_id=conversation_id,
                event_type="voice_transcription",
                channel="telegram",
                description="Owner voice message transcription"
            )

        except Exception as e:

            print(
                "OWNER VOICE CREDIT CONSUMPTION FAILED:",
                repr(e)
            )

            return


    if not text or not text.strip():
        return


    try:

        consume_bridge_credits(
            business_id=business_id,
            credits=1,
            conversation_id=conversation_id,
            event_type="translation",
            channel="telegram",
            description="Owner message translation"
        )

    except Exception as e:

        print(
            "OWNER TRANSLATION CREDIT CONSUMPTION FAILED:",
            repr(e)
        )

        await send_message(
            owner_chat_id,
            "I'm sorry, but I can't process this message right now. Please try again later."
        )

        return

    result = translate(
        text=text,
        target_language=customer_language
    )

    translated_text = result["translated_text"]


    if was_voice:

        save_usage_event(
            business_id=business_id,
            conversation_id=conversation_id,
            event_type="voice_transcription",
            channel="telegram",
            language=customer_language
        )

    save_usage_event(
        business_id=business_id,
        conversation_id=conversation_id,
        event_type="translation",
        channel="telegram",
        language=customer_language
    )

    save_usage_event(
        business_id=business_id,
        conversation_id=conversation_id,
        event_type="owner_message",
        channel="telegram",
        language=customer_language
    )


    save_bridge_message(
        conversation_id=conversation_id,
        sender_type="owner",
        original_text=text,
        translated_text=translated_text,
        language=customer_language,
        response_source="ai"
    )

    update_conversation_timestamp(
        conversation_id
    )


    await send_message(
        customer_chat_id,
        translated_text
    )


    await send_message(
        owner_chat_id,
        f"✅ Message sent to {customer.get('name') or 'customer'} in {customer_language}."
    )

async def handle_customers_command(
    owner_chat_id: int,
    business: dict
):
    business_id = business["id"]

    response = (
        supabase
        .table(BRIDGE_CUSTOMERS_TABLE)
        .select(
            "id, name, telegram_chat_id, language, last_seen_at"
        )
        .eq("business_id", business_id)
        .order("last_seen_at", desc=True)
        .execute()
    )

    customers = response.data or []

    if not customers:
        await send_message(
            owner_chat_id,
            "There are no customers yet."
        )
        return

    lines = ["👥 Your Customers\n"]

    for index, customer in enumerate(customers, start=1):

        name = customer.get("name") or "Customer"
        language = customer.get("language") or "Unknown"

        lines.append(
            f"{index}. {name}\n"
            f"   Language: {language}"
        )

    await send_message(
        owner_chat_id,
        "\n\n".join(lines)
    )

def save_usage_event(
    business_id: str,
    conversation_id: str,
    event_type: str,
    channel: str,
    language: str | None = None
):
    supabase.table("atupcy_bridge_usage_events").insert(
        {
            "business_id": business_id,
            "conversation_id": conversation_id,
            "event_type": event_type,
            "channel": channel,
            "language": language
        }
    ).execute()

def consume_bridge_credits(
    business_id: str,
    credits: int,
    conversation_id: str | None = None,
    event_type: str | None = None,
    channel: str | None = None,
    description: str | None = None
):
    response = supabase.rpc(
        "consume_bridge_credits",
        {
            "p_business_id": business_id,
            "p_credits": credits,
            "p_conversation_id": conversation_id,
            "p_event_type": event_type,
            "p_channel": channel,
            "p_description": description
        }
    ).execute()

    return response.data

def check_bridge_credits(
    business_id: str,
    credits: int
):
    response = supabase.rpc(
        "check_bridge_credits",
        {
            "p_business_id": business_id,
            "p_credits": credits
        }
    ).execute()

    return response.data

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

async def send_to_ai_support(
    conversation_id: str,
    message: str
):
    payload = {
        "sessionId": conversation_id,
        "message": message,
        "email": ""
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            AI_SUPPORT_WEBHOOK_URL,
            json=payload,
            timeout=60
        )

    response.raise_for_status()
    return response.json()

async def transcribe_voice(file_id: str) -> str:

    async with httpx.AsyncClient() as client:

        
        file_info_response = await client.get(
            f"{TELEGRAM_API_URL}/getFile",
            params={
                "file_id": file_id
            }
        )

        print(
            "TELEGRAM GETFILE STATUS:",
            file_info_response.status_code
        )

        print(
            "TELEGRAM GETFILE RESPONSE:",
            file_info_response.text
        )

        file_info_response.raise_for_status()

        try:
            file_data = file_info_response.json()
        except Exception as e:
            print(
                "TELEGRAM GETFILE JSON ERROR:",
                repr(e)
            )
            raise

        file_path = file_data["result"]["file_path"]

        print(
            "TELEGRAM FILE PATH:",
            file_path
        )

    
        file_url = (
            f"https://api.telegram.org/file/bot"
            f"{BOT_TOKEN}/{file_path}"
        )

        audio_response = await client.get(file_url)

        print(
            "TELEGRAM AUDIO STATUS:",
            audio_response.status_code
        )

        print(
            "TELEGRAM AUDIO CONTENT TYPE:",
            audio_response.headers.get("content-type")
        )

        audio_response.raise_for_status()

        audio_bytes = audio_response.content

    print(
        "VOICE FILE SIZE:",
        len(audio_bytes)
    )

    if not audio_bytes:
        raise ValueError(
            "Downloaded voice file was empty"
        )

    
    try:

        print(
            "OPENAI TRANSCRIPTION STARTING"
        )

        transcription = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=(
                "voice.ogg",
                audio_bytes,
                "audio/ogg"
            )
        )

        print(
            "OPENAI TRANSCRIPTION SUCCESS:",
            transcription.text
        )

    except Exception as e:

        print(
            "OPENAI TRANSCRIPTION ERROR:",
            repr(e)
        )

        raise

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