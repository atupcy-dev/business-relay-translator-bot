import os
import json
import traceback
import httpx
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from openai import OpenAI
from supabase import create_client, Client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("atupcy_bridge")

load_dotenv()

app = FastAPI()

@app.on_event("startup")
async def recover_stale_executions_on_startup():
    try:
        recover_stale_bridge_executions()
        logger.info("Stale Bridge execution recovery completed")
    except Exception as e:
        logger.error(
            "Stale Bridge execution recovery failed | error=%r",
            e
        )

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

async def send_language_selection(
    customer_chat_id: int
):

    reply_markup = {
        "inline_keyboard": [
            [
                {
                    "text": "🇬🇧 English",
                    "callback_data": "language:English"
                },
                {
                    "text": "🇫🇷 Français",
                    "callback_data": "language:French"
                }
            ],
            [
                {
                    "text": "🇪🇸 Español",
                    "callback_data": "language:Spanish"
                },
                {
                    "text": "🇩🇪 Deutsch",
                    "callback_data": "language:German"
                }
            ],
            [
                {
                    "text": "🇦🇪 العربية",
                    "callback_data": "language:Arabic"
                },
                {
                    "text": "🇨🇳 中文",
                    "callback_data": "language:Chinese"
                }
            ],
            [
                {
                    "text": "🇯🇵 日本語",
                    "callback_data": "language:Japanese"
                },
                {
                    "text": "🔎 Search for another language",
                    "callback_data": "language:search"
                }
            ]
        ]
    }

    await send_message(
        customer_chat_id,
        "🌍 Welcome!\n\n"
        "Please choose your preferred language.\n\n"
        "If you don't see your language, "
        "tap 🔎 Search for another language.",
        reply_markup=reply_markup
    )

async def request_with_retry(
    client,
    method: str,
    url: str,
    retries: int = 2,
    **kwargs
):
    last_error = None

    for attempt in range(retries + 1):

        try:
            response = await client.request(
                method,
                url,
                **kwargs
            )

            # Retry only safe transient HTTP failures
            if response.status_code == 429 or 500 <= response.status_code < 600:

                if attempt >= retries:
                    response.raise_for_status()

                retry_after = response.headers.get("Retry-After")

                if retry_after:
                    try:
                        wait_time = float(retry_after)
                    except ValueError:
                        wait_time = 2 ** attempt
                else:
                    wait_time = 2 ** attempt

                logger.warning(
                    "Transient HTTP error | "
                    "method=%s | url=%s | status=%s | "
                    "retry_in=%ss | attempt=%s/%s",
                    method,
                    url,
                    response.status_code,
                    wait_time,
                    attempt + 1,
                    retries
                )

                await asyncio.sleep(wait_time)
                continue

            response.raise_for_status()

            return response

        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.ConnectError
        ) as e:

            last_error = e

            if attempt >= retries:
                raise

            wait_time = 2 ** attempt

            logger.warning(
                "HTTP request failed | "
                "method=%s | url=%s | error=%r | "
                "retry_in=%ss | attempt=%s/%s",
                method,
                url,
                e,
                wait_time,
                attempt + 1,
                retries
            )

            await asyncio.sleep(wait_time)

    if last_error:
        raise last_error

    raise RuntimeError("HTTP request failed unexpectedly")


@app.get("/")
async def health_check():
    return {
        "status": "ok",
        "message": "Atupcy Bridge is running"
    }


@app.get("/telegram-webhook-info")
async def telegram_webhook_info():
    async with httpx.AsyncClient() as client:
        response = await request_with_retry(
            client,
            "GET",
            f"{TELEGRAM_API_URL}/getWebhookInfo",
            timeout=30
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
        "channel": "telegram",
        "handling_mode": "ai",
        "handoff_status": "none"
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

def claim_telegram_update(
    update_id: int
) -> bool:

    response = (
        supabase
        .rpc(
            "claim_telegram_update",
            {
                "p_update_id": update_id
            }
        )
        .execute()
    )

    return bool(response.data)

def complete_telegram_update(
    update_id: int
) -> bool:

    response = (
        supabase
        .rpc(
            "complete_telegram_update",
            {
                "p_update_id": update_id
            }
        )
        .execute()
    )

    return bool(response.data)

def start_bridge_execution(
    operation: str,
    update_id: int | None = None,
    business_id: str | None = None,
    conversation_id: str | None = None
):
    response = (
        supabase
        .table("atupcy_bridge_executions")
        .insert({
            "operation": operation,
            "update_id": update_id,
            "business_id": business_id,
            "conversation_id": conversation_id,
            "status": "started"
        })
        .execute()
    )

    rows = response.data or []

    return rows[0]["id"] if rows else None

def finish_bridge_execution(
    execution_id: str,
    status: str = "completed",
    error_type: str | None = None,
    error_message: str | None = None
):
    if status not in ("completed", "failed"):
        raise ValueError("Invalid execution status")

    execution = (
        supabase
        .table("atupcy_bridge_executions")
        .select("started_at")
        .eq("id", execution_id)
        .limit(1)
        .execute()
    )

    rows = execution.data or []

    if not rows:
        return None

    started_at = rows[0]["started_at"]

    started = datetime.fromisoformat(
        started_at.replace("Z", "+00:00")
    )

    completed_at = datetime.now(timezone.utc)

    duration_ms = int(
        (
            completed_at - started
        ).total_seconds() * 1000
    )

    response = (
        supabase
        .table("atupcy_bridge_executions")
        .update({
            "status": status,
            "completed_at": completed_at.isoformat(),
            "duration_ms": duration_ms,
            "error_type": error_type,
            "error_message": error_message
        })
        .eq("id", execution_id)
        .execute()
    )

    rows = response.data or []

    return rows[0] if rows else None

def recover_stale_bridge_executions(
    max_age_minutes: int = 10
):
    cutoff_time = (
        datetime.now(timezone.utc)
        - timedelta(minutes=max_age_minutes)
    ).isoformat()

    response = (
        supabase
        .table("atupcy_bridge_executions")
        .update({
            "status": "failed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "error_type": "stale_execution",
            "error_message": (
                "Execution remained in started state "
                "beyond the recovery threshold."
            )
        })
        .eq("status", "started")
        .lt("started_at", cutoff_time)
        .execute()
    )

    rows = response.data or []

    if rows:
        logger.warning(
            "Recovered stale Bridge executions | count=%s",
            len(rows)
        )

    return rows

async def finish_telegram_update(
    update_id,
    execution_id=None,
    status="completed",
    error_type=None,
    error_message=None
):
    if update_id is not None:
        try:
            complete_telegram_update(update_id)

        except Exception as e:
            logger.error(
                "Failed to complete Telegram update | "
                "update_id=%s | error=%r",
                update_id,
                e
            )

        finally:
            if execution_id:
                logger.info(
                    "Finalizing Bridge execution | "
                    "execution_id=%s | status=%s",
                    execution_id,
                    status
                )

                try:
                    result = finish_bridge_execution(
                        execution_id=execution_id,
                        status=status,
                        error_type=error_type,
                        error_message=error_message
                    )

                    logger.info(
                        "Bridge execution finalized | "
                        "execution_id=%s | result=%s",
                        execution_id,
                        result
                    )

                except Exception as e:
                    logger.error(
                        "Bridge execution finalization failed | "
                        "execution_id=%s | error=%r",
                        execution_id,
                        e,
                        exc_info=True
                    )

    return {"ok": True}

@app.post("/webhook")
async def webhook(request: Request):

    update = await request.json()

    recover_stale_bridge_executions()

    print("TELEGRAM UPDATE:", update)

    update_id = update.get("update_id")

    if update_id is not None:

        try:

            claimed = claim_telegram_update(
                update_id
            )

            if not claimed:

                logger.warning(
                    "Duplicate Telegram update ignored | update_id=%s",
                    update_id
                )

                return {"ok": True}

        except Exception as e:

            logger.error(
                "Idempotency claim failed in webhook | error=%r",
                e
            )

            return {"ok": True}

        execution_id = start_bridge_execution(
            operation="telegram_webhook",
            update_id=update_id,
            business_id=None,
            conversation_id=None
        )

        callback_query = update.get("callback_query")

    if callback_query:

        callback_data = callback_query.get("data")

        callback_message = (
            callback_query.get("message") or {}
        )

        callback_chat = (
            callback_message.get("chat") or {}
        )

        callback_chat_id = callback_chat.get("id")


        if (
            callback_data
            and callback_data.startswith("language:")
            and callback_chat_id
        ):

            selected_language = callback_data.split(
                "language:",
                1
            )[1]

            if selected_language == "search":

                (
                    supabase
                    .table(BRIDGE_CUSTOMERS_TABLE)
                    .update({
                        "language_search_pending": True
                    })
                    .eq(
                        "telegram_chat_id",
                        callback_chat_id
                    )
                    .execute()
                    )

                await answer_callback_query(
                    callback_query["id"]
                )

                await send_message(
                    callback_chat_id,
                    "🔎 Please type the name of your preferred language."
                )

                return await finish_telegram_update(
                    update_id,
                    execution_id=execution_id
                )

            (
                supabase
                .table(BRIDGE_CUSTOMERS_TABLE)
                .update({
                    "language": selected_language
                })
                .eq(
                    "telegram_chat_id",
                    callback_chat_id
                )
                .execute()
            )

            await answer_callback_query(
                callback_query["id"]
            )

            await send_message(
                callback_chat_id,
                get_language_confirmation(
                    selected_language
                )
            )

            return await finish_telegram_update(
                update_id,
                execution_id=execution_id)


        if (
            callback_data
            and callback_data.startswith("human_reply:")
            and callback_chat_id
        ):

            conversation_id = callback_data.split(
                "human_reply:",
                1
            )[1]

            business = get_active_business()

            if not business:
                return await finish_telegram_update(
                    update_id,
                    execution_id=execution_id)

            owner_chat_id = business.get(
                "owner_chat_id"
            )

            if not owner_chat_id:
                return await finish_telegram_update(
                    update_id,
                    execution_id=execution_id)

            owner_chat_id = int(owner_chat_id)

            # Only the business owner can take over
            if callback_chat_id != owner_chat_id:
                return await finish_telegram_update(
                    update_id,
                    execution_id=execution_id)

            conversation = get_conversation_by_id(
                conversation_id
            )

            if not conversation:

                await send_message(
                    owner_chat_id,
                    "That conversation could not be found."
                )

                return await finish_telegram_update(
                    update_id,
                    execution_id=execution_id
                    )

            # Make sure the conversation belongs
            # to this business

            if conversation.get("business_id") != business["id"]:

                await send_message(
                    owner_chat_id,
                    "That conversation does not belong to this business."
                )

                return await finish_telegram_update(
                    update_id,
                    execution_id=execution_id
                    )

            # Conversation must still be active

            if conversation.get("status") != "active":

                await send_message(
                    owner_chat_id,
                    "That conversation is no longer active."
                )

                return await finish_telegram_update(
                    update_id,
                    execution_id=execution_id
                )

            # Switch conversation to human handling

            update_conversation_handling_mode(
                conversation_id=conversation_id,
                handling_mode="human"
            )

            update_conversation_handoff(
                conversation_id=conversation_id,
                handoff_status="accepted"
            )

            # Make this the owner's selected conversation

            set_owner_selected_conversation(
                business_id=business["id"],
                owner_chat_id=owner_chat_id,
                conversation_id=conversation_id
            )

            customer = get_customer_by_id(
                conversation["customer_id"]
            )

            customer_name = (
                customer.get("name")
                if customer
                else "Customer"
            )

            await answer_callback_query(
                callback_query["id"]
            )

            await send_message(
                owner_chat_id,
                f"👤 Human mode activated\n\n"
                f"Customer: {customer_name}\n\n"
                f"Your next message will be sent directly "
                f"to this customer."
            )

            return await finish_telegram_update(
                update_id,
                execution_id=execution_id)


        if (
            callback_data
            and callback_data.startswith("select_customer:")
            and callback_chat_id
        ):

            customer_id = callback_data.split(
                "select_customer:",
                1
            )[1]

            business = get_active_business()

            if not business:
                return await finish_telegram_update(
                    update_id,
                    execution_id=execution_id)

            owner_chat_id = business.get(
                "owner_chat_id"
            )

            if not owner_chat_id:
                return await finish_telegram_update(
                    update_id,
                    execution_id=execution_id
                )

            owner_chat_id = int(owner_chat_id)

            if callback_chat_id != owner_chat_id:
                return await finish_telegram_update(
                    update_id,
                    execution_id=execution_id
                )

            customer = get_customer_by_id(
                customer_id
            )

            if not customer:

                await send_message(
                    owner_chat_id,
                    "That customer could not be found."
                )

                return await finish_telegram_update(
                    update_id,
                    execution_id=execution_id
                )

            conversation = get_or_create_conversation(
                business_id=business["id"],
                customer_id=customer_id
            )

            set_owner_selected_conversation(
                business_id=business["id"],
                owner_chat_id=owner_chat_id,
                conversation_id=conversation["id"]
            )

            customer_name = (
                customer.get("name")
                or "Customer"
            )

            customer_language = (
                customer.get("language")
                or "Unknown"
            )

            await send_message(
                owner_chat_id,
                f"✅ Customer selected\n\n"
                f"Customer: {customer_name}\n"
                f"Language: {customer_language}\n\n"
                f"Your next message will be sent to this customer."
            )

            return await finish_telegram_update(
                update_id,
                execution_id=execution_id)


        return await finish_telegram_update(
            update_id,
            execution_id=execution_id)

    message = update.get("message")

    if not message:

        return await finish_telegram_update(
            update_id,
            execution_id=execution_id)

    chat = message.get("chat") or {}

    chat_id = chat.get("id")

    if not chat_id:
        return await finish_telegram_update(
            update_id,
            execution_id=execution_id)

    customer_name = (
        chat.get("first_name")
        or chat.get("username")
        or "Telegram User"
    )

    text = message.get("text")
    voice = message.get("voice")

    if not text and not voice:
        return await finish_telegram_update(
            update_id,
            execution_id=execution_id)

    business = get_active_business()

    if not business:

        await send_message(
            chat_id,
            "Atupcy Bridge is not currently connected to an active business."
        )


        return await finish_telegram_update(
            update_id,
            execution_id=execution_id)

    owner_chat_id = business.get(
        "owner_chat_id"
    )

    if not owner_chat_id:

        print(
            "ERROR: Active business has no owner_chat_id"
        )

        await send_message(
            chat_id,
            "Atupcy Bridge is not fully configured yet."
        )

        return await finish_telegram_update(
            update_id,
            execution_id=execution_id)

    owner_chat_id = int(owner_chat_id)


    if chat_id == owner_chat_id:


        if (
            text
            and text.strip().lower()
            == "/customers"
        ):

            try:

                await handle_customers_command(
                    owner_chat_id=owner_chat_id,
                    business=business
                )

            except Exception as e:

                logger.error(
                    "Customers command failed | error=%r",
                    e
                )

                await send_message(
                    owner_chat_id,
                    "Sorry, something went wrong while loading your customers."
                )

                return await finish_telegram_update(
                    update_id,
                    execution_id=execution_id)

        # /CURRENT COMMAND

        if text and text.strip().lower() == "/current":

            try:

                await handle_current_command(
                    owner_chat_id=owner_chat_id,
                    business=business
                )

            except Exception as e:

                logger.error(
                    "Current command error | error=%r",
                    e
                )

                await send_message(
                    owner_chat_id,
                    "Sorry, something went wrong while checking the current customer."
                )

            return await finish_telegram_update(
                update_id,
                execution_id=execution_id)

        # /CLOSE COMMAND

        if text and text.strip().lower() == "/close":

            try:

                await handle_close_command(
                    owner_chat_id=owner_chat_id,
                    business=business
                )

            except Exception as e:

                logger.error(
                    "Close command error | error=%r",
                    e
                )

                await send_message(
                    owner_chat_id,
                    "Sorry, something went wrong while closing the conversation."
                )

            return await finish_telegram_update(
                update_id,
                execution_id=execution_id)

        # /AI COMMAND
        if text and text.strip().lower() == "/ai":

            try:

                business = get_active_business()

                if not business:
                    await send_message(
                        owner_chat_id,
                        "No active Atupcy Bridge business found."
                    )
                    return await finish_telegram_update(
                        update_id,
                        execution_id=execution_id
                    )

                business_id = business["id"]

                selected_conversation = get_owner_selected_conversation(
                                    business_id=business_id,
                                    owner_chat_id=owner_chat_id
                                )

                if not selected_conversation:
                    await send_message(
                        owner_chat_id,
                        "No customer is currently selected.\n\n"
                        "Use /customers to select a customer first."
                    )
                    return await finish_telegram_update(
                        update_id,
                        execution_id=execution_id
                    )

                conversation_id = selected_conversation["id"]

                if selected_conversation.get("status") != "active":
                    clear_owner_selected_conversation(
                        business_id=business_id,
                        owner_chat_id=owner_chat_id
                    )

                    await send_message(
                        owner_chat_id,
                        "This conversation is already closed.\n\n"
                        "Use /customers to select an active customer."
                    )
                    return await finish_telegram_update(
                        update_id,
                        execution_id=execution_id
                    )

                update_conversation_handling_mode(
                    conversation_id=conversation_id,
                    handling_mode="ai"
                )

                update_conversation_handoff(
                    conversation_id=conversation_id,
                    handoff_status="none"
                )

                customer = get_customer_by_id(
                    selected_conversation["customer_id"]
                )

                customer_display_name = (
                    customer.get("name")
                    if customer
                    else "Customer"
                )

                await send_message(
                    owner_chat_id,
                    f"🤖 AI mode activated\n\n"
                    f"Customer: {customer_display_name}\n\n"
                    "New customer messages will now be handled by the AI."
                )

            except Exception as e:

                logger.error(
                    "AI mode switch failed | error=%r",
                    e
                )

                await send_message(
                    owner_chat_id,
                    "I'm sorry, but I couldn't switch this conversation "
                    "back to AI mode right now. Please try again later."
                )

                return await finish_telegram_update(
                    update_id, 
                    execution_id=execution_id)

        if text and text.strip().lower() == "/history":

            try:

                await handle_history_command(
                    owner_chat_id=owner_chat_id,
                    business=business
                )

            except Exception as e:

                logger.error(
                    "History command error | error=%r",
                    e
                )

                await send_message(
                    owner_chat_id,
                    "Sorry, something went wrong while loading the conversation history."
                )

                return await finish_telegram_update(
                    update_id,
                    execution_id=execution_id)

        try:

            await handle_owner_message(
                owner_chat_id=owner_chat_id,
                business=business,
                text=text,
                voice=voice
            )


        except Exception as e:

            logger.error(
                "Owner message error | error=%r",
                e
            )

            await send_message(
                owner_chat_id,
                "Sorry, something went wrong while processing your message."
            )

        return await finish_telegram_update(
            update_id,
            execution_id=execution_id)


    if (
        text
        and text.strip().lower() in (
            "/start",
            "/language"
        )
    ):

        await send_language_selection(
            customer_chat_id=chat_id
        )

        return await finish_telegram_update(
            update_id,
            execution_id=execution_id
        )

    # CUSTOMER LANGUAGE SEARCH

    if text:

        customer_response = (
            supabase
            .table(BRIDGE_CUSTOMERS_TABLE)
            .select("id, language_search_pending")
            .eq(
                "telegram_chat_id",
                chat_id
            )
            .limit(1)
            .execute()
        )

        customer_rows = customer_response.data or []

        if customer_rows:

            customer = customer_rows[0]

            if customer.get("language_search_pending"):

                selected_language = text.strip()

                if not selected_language:
                    await send_message(
                        chat_id,
                        "Please type a language name."
                    )

                    return await finish_telegram_update(
                        update_id,
                        execution_id=execution_id
                    )

                (
                    supabase
                    .table(BRIDGE_CUSTOMERS_TABLE)
                    .update({
                        "language": selected_language,
                        "language_search_pending": False
                    })
                    .eq(
                        "id",
                        customer["id"]
                    )
                    .execute()
                )

                await send_message(
                    chat_id,
                    get_language_confirmation(
                        selected_language
                    )
                )

                return await finish_telegram_update(
                    update_id,
                    execution_id=execution_id
                )

    try:

        if text == "__BRIDGE_FAILURE_TEST__":
            raise RuntimeError(
                "Intentional Bridge execution failure test."
            )

        await handle_customer_message(
            customer_chat_id=chat_id,
            customer_name=customer_name,
            business=business,
            text=text,
            voice=voice
        )

    except Exception as e:

        logger.error(
            "Customer message error | error=%r",
            e
        )

        traceback.print_exc()

        await send_message(
            chat_id,
            "Sorry, something went wrong while processing your message."
        )

        return await finish_telegram_update(
            update_id,
            execution_id=execution_id,
            status="failed",
            error_type=type(e).__name__,
            error_message=str(e)
        )

    return await finish_telegram_update(
        update_id,
        execution_id=execution_id
    )

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

def get_language_confirmation(language: str) -> str:

    confirmations = {

        "English":
            "✅ Your preferred language is now set to English.\n\n"
            "You can change it anytime.",

        "French":
            "✅ Votre langue préférée est maintenant définie sur le français.\n\n"
            "Vous pouvez la modifier à tout moment.",

        "Spanish":
            "✅ Tu idioma preferido ahora está configurado en español.\n\n"
            "Puedes cambiarlo en cualquier momento.",

        "German":
            "✅ Ihre bevorzugte Sprache ist jetzt Deutsch.\n\n"
            "Sie können sie jederzeit ändern.",

        "Arabic":
            "✅ تم تعيين لغتك المفضلة الآن على العربية.\n\n"
            "يمكنك تغييرها في أي وقت.",

        "Chinese":
            "✅ 您的首选语言现已设置为中文。\n\n"
            "您可以随时更改。",

        "Japanese":
            "✅ ご希望の言語が日本語に設定されました。\n\n"
            "いつでも変更できます。",

        "Yoruba":
            "✅ Èdè tí o fẹ́ràn ti ṣètò sí Yorùbá.\n\n"
            "O lè yí i padà nígbàkigbà.",
    }

    return confirmations.get(
        language,
        f"✅ Your preferred language is now set to {language}.\n\n"
        "You can change it anytime."
    )

def get_credit_limit_message(language: str) -> str:

    messages = {

        "English":
            "We're temporarily unable to process your request. "
            "Please try again later.",

        "French":
            "Nous ne pouvons temporairement pas traiter votre demande. "
            "Veuillez réessayer plus tard.",

        "Spanish":
            "No podemos procesar tu solicitud temporalmente. "
            "Por favor, inténtalo de nuevo más tarde.",

        "German":
            "Wir können Ihre Anfrage derzeit vorübergehend nicht bearbeiten. "
            "Bitte versuchen Sie es später erneut.",

        "Arabic":
            "يتعذر علينا معالجة طلبك مؤقتًا. "
            "يرجى المحاولة مرة أخرى لاحقًا.",

        "Chinese":
            "我们暂时无法处理您的请求。"
            "请稍后再试。",

        "Japanese":
            "現在、一時的にリクエストを処理できません。"
            "後でもう一度お試しください。",

        "Yoruba":
            "A kò lè ṣe ìbéèrè rẹ fún àkókò díẹ̀. "
            "Jọ̀wọ́ tún gbìyànjú lẹ́yìn náà.",
    }

    return messages.get(
        language,
        messages["English"]
    )

def get_processing_error_message(language: str) -> str:

    messages = {

        "English":
            "I'm sorry, but I'm unable to process your "
            "request right now. Please try again later.",

        "French":
            "Je suis désolé, mais je ne peux pas traiter "
            "votre demande pour le moment. Veuillez réessayer plus tard.",

        "Spanish":
            "Lo siento, pero no puedo procesar tu solicitud "
            "en este momento. Por favor, inténtalo de nuevo más tarde.",

        "German":
            "Es tut mir leid, aber ich kann Ihre Anfrage "
            "im Moment nicht bearbeiten. Bitte versuchen Sie es später erneut.",

        "Arabic":
            "عذرًا، لا يمكنني معالجة طلبك في الوقت الحالي. "
            "يرجى المحاولة مرة أخرى لاحقًا.",

        "Chinese":
            "很抱歉，我目前无法处理您的请求。"
            "请稍后再试。",

        "Japanese":
            "申し訳ありませんが、現在リクエストを処理できません。"
            "後でもう一度お試しください。",

        "Yoruba":
            "Má bínú, ṣùgbọ́n mi ò lè ṣe ìbéèrè rẹ "
            "ní àkókò yìí. Jọ̀wọ́ tún gbìyànjú lẹ́yìn náà.",
    }

    return messages.get(
        language,
        messages["English"]
    )

def get_voice_processing_error_message(language: str) -> str:

    messages = {

        "English":
            "I'm sorry, but I can't process your "
            "voice message right now. Please try again later.",

        "French":
            "Je suis désolé, mais je ne peux pas traiter "
            "votre message vocal pour le moment. Veuillez réessayer plus tard.",

        "Spanish":
            "Lo siento, pero no puedo procesar tu "
            "mensaje de voz en este momento. Por favor, inténtalo de nuevo más tarde.",

        "German":
            "Es tut mir leid, aber ich kann Ihre "
            "Sprachnachricht derzeit nicht verarbeiten. Bitte versuchen Sie es später erneut.",

        "Arabic":
            "عذرًا، لا يمكنني معالجة رسالتك الصوتية "
            "في الوقت الحالي. يرجى المحاولة مرة أخرى لاحقًا.",

        "Chinese":
            "很抱歉，我目前无法处理您的语音消息。"
            "请稍后再试。",

        "Japanese":
            "申し訳ありませんが、現在音声メッセージを処理できません。"
            "後でもう一度お試しください。",

        "Yoruba":
            "Má bínú, ṣùgbọ́n mi ò lè ṣe ìfiránṣẹ́ ohùn rẹ "
            "ní àkókò yìí. Jọ̀wọ́ tún gbìyànjú lẹ́yìn náà.",
    }

    return messages.get(
        language,
        messages["English"]
    )

def get_customer_fallback_message(language: str) -> str:

    messages = {

        "English":
            "I'm sorry, but I can't process your message right now. "
            "Please try again later.",

        "French":
            "Je suis désolé, mais je ne peux pas traiter votre message "
            "pour le moment. Veuillez réessayer plus tard.",

        "Spanish":
            "Lo siento, pero no puedo procesar tu mensaje en este momento. "
            "Por favor, inténtalo de nuevo más tarde.",

        "German":
            "Es tut mir leid, aber ich kann Ihre Nachricht derzeit nicht "
            "verarbeiten. Bitte versuchen Sie es später erneut.",

        "Arabic":
            "عذرًا، لا يمكنني معالجة رسالتك في الوقت الحالي. "
            "يرجى المحاولة مرة أخرى لاحقًا.",

        "Chinese":
            "很抱歉，我目前无法处理您的消息。"
            "请稍后再试。",

        "Japanese":
            "申し訳ありませんが、現在メッセージを処理できません。"
            "後でもう一度お試しください。",

        "Yoruba":
            "Má bínú, ṣùgbọ́n mi ò lè ṣe ìfiránṣẹ́ rẹ "
            "ní àkókò yìí. Jọ̀wọ́ tún gbìyànjú lẹ́yìn náà.",
    }

    return messages.get(
        language,
        messages["English"]
    )

def get_voice_transcription_error_message(language: str) -> str:

    messages = {

        "English":
            "I couldn't make out any speech in that voice note. "
            "Please try again.",

        "French":
            "Je n'ai pas pu comprendre la parole dans ce message vocal. "
            "Veuillez réessayer.",

        "Spanish":
            "No pude entender el audio de esa nota de voz. "
            "Por favor, inténtalo de nuevo.",

        "German":
            "Ich konnte in dieser Sprachnachricht keine Sprache erkennen. "
            "Bitte versuchen Sie es erneut.",

        "Arabic":
            "لم أتمكن من فهم الكلام في هذه الرسالة الصوتية. "
            "يرجى المحاولة مرة أخرى.",

        "Chinese":
            "我无法听清这条语音消息中的内容。"
            "请再试一次。",

        "Japanese":
            "この音声メッセージの内容を聞き取れませんでした。"
            "もう一度お試しください。",

        "Yoruba":
            "Mi ò lè gbọ́ ohun tí o sọ nínú àkọsílẹ̀ ohùn yìí. "
            "Jọ̀wọ́ tún gbìyànjú.",
    }

    return messages.get(
        language,
        messages["English"]
    )

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

    handling_mode = (
        conversation.get("handling_mode")
        or "ai"
    )

    print(
        "ATUPCY BRIDGE CONVERSATION:",
        conversation
    )

    if not text and not voice:
        return


    if handling_mode == "human":
        required_credits = 4 if voice else 1
    else:
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
                get_credit_limit_message(
                    customer.get("language") or "English"
                )
            )


            customer_language_for_owner = (
                customer.get("language")
                or "Unknown"
            )

            owner_customer_message = (
                f"📩 Customer Message\n\n"
                f"Customer: {customer_name}\n"
                f"Language: {customer_language_for_owner}\n\n"
                f"{text or '🎙️ Voice message'}"
            )

            await send_message(
                owner_chat_id,
                owner_customer_message
            )

            await send_message(
                owner_chat_id,
                f"🚨 Bridge Usage Limit Reached\n\n"
                f"Customer: {customer_name}\n"
                f"Language: {customer_language_for_owner}\n\n"
                f"The customer’s message could not be "
                f"processed because your available Bridge "
                f"usage has been exhausted.\n\n"
                f"Please handle this customer manually or "
                f"contact Atupcy LTD to continue."
            )

            return

    except Exception as e:

        logger.error(
            "Customer credit check failed | error=%r",
            e
        )

        await send_message(
            customer_chat_id,
            get_processing_error_message(
                customer.get("language") or "English"
            )
        )

        return


    if voice:

        text = await transcribe_voice(
            voice["file_id"]
        )

        if not text or not text.strip():

            await send_message(
                customer_chat_id,
                get_voice_transcription_error_message(
                    customer.get("language") or "English"
                )
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

            logger.error(
                "Voice credit consumption failed | error=%r",
                e
            )

            await send_message(
                customer_chat_id,
                get_voice_processing_error_message(
                    customer.get("language") or "English"
                )
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

        logger.error(
            "Customer translation credit consumption failed | error=%r",
            e
        )

        await send_message(
            customer_chat_id,
            get_customer_fallback_message(
                customer.get("language") or "English"
            )
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
        response_source=(
            "human"
            if handling_mode == "human"
            else "ai"
        )
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


    if handling_mode == "human":

        await send_message(
            owner_chat_id,
            "👤 Human mode\n\n"
            "This customer is currently being handled "
            "by you.\n"
            "Reply to the customer directly."
        )

        return


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

        logger.error(
            "AI support credit consumption failed | error=%r",
            e
        )

        await send_message(
            customer_chat_id,
            get_processing_error_message(
                customer.get("language") or "English"
            )
        )

        await send_message(
            owner_chat_id,
            f"🚨 Bridge Usage Limit Reached\n\n"
            f"Customer: {customer_display_name}\n"
            f"Language: {source_language}\n\n"
            f"The customer’s message could not be "
            f"processed because your available Bridge "
            f"usage has been exhausted.\n\n"
            f"Please handle this customer manually or "
            f"contact Atupcy LTD to continue."
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

            logger.error(
                "AI response translation credit consumption failed | error=%r",
                e
            )

            await send_message(
                customer_chat_id,
                get_customer_fallback_message(
                    customer.get("language") or "English"
                )
            )

            return

        ai_translation_result = translate(
            text=ai_reply,
            target_language=customer.get("language") or source_language
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

        update_conversation_handoff(
            conversation_id=conversation_id,
            handoff_status="offered",
            escalation_reason=(
                "AI support agent flagged the "
                "conversation for human review."
            )
        )

        reply_markup = {
            "inline_keyboard": [
                [
                    {
                        "text": (
                            f"💬 Reply to "
                            f"{customer_display_name}"
                        ),
                        "callback_data": (
                            f"human_reply:"
                            f"{conversation_id}"
                        )
                    }
                ]
            ]
        }

        await send_message(
            owner_chat_id,
            f"🚨 AI Support Escalation\n\n"
            f"Customer: {customer_display_name}\n"
            f"Language: {source_language}\n\n"
            f"The AI support agent has flagged this "
            f"conversation for human review.\n\n"
            f"Tap below to take over this conversation.",
            reply_markup=reply_markup
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

def update_conversation_handling_mode(
    conversation_id: str,
    handling_mode: str
):
    """
    Change who is currently handling the conversation.

    handling_mode:
        - ai
        - human
    """

    if handling_mode not in ("ai", "human"):
        raise ValueError(
            "Invalid handling mode"
        )

    response = (
        supabase
        .table(BRIDGE_CONVERSATIONS_TABLE)
        .update(
            {
                "handling_mode": handling_mode
            }
        )
        .eq("id", conversation_id)
        .execute()
    )

    rows = response.data or []

    return rows[0] if rows else None

def update_conversation_handoff(
    conversation_id: str,
    handoff_status: str,
    escalation_reason: str | None = None
):
    """
    Update the human handoff state.

    handoff_status:
        - none
        - offered
        - accepted
        - declined
    """

    if handoff_status not in (
        "none",
        "offered",
        "accepted",
        "declined"
    ):
        raise ValueError(
            "Invalid handoff status"
        )

    data = {
        "handoff_status": handoff_status
    }

    if escalation_reason is not None:
        data["escalation_reason"] = escalation_reason

    response = (
        supabase
        .table(BRIDGE_CONVERSATIONS_TABLE)
        .update(data)
        .eq("id", conversation_id)
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


    conversation = get_owner_selected_conversation(
        business_id=business_id,
        owner_chat_id=owner_chat_id
    )

    if not conversation:

        await send_message(
            owner_chat_id,
            "No customer is currently selected.\n\n"
            "Use /customers to select a customer before sending a message."
        )

        return

    if conversation.get("status") != "active":
        clear_owner_selected_conversation(
            business_id=business_id,
            owner_chat_id=owner_chat_id
        )

        await send_message(
            owner_chat_id,
            "The selected conversation is no longer active.\n\n"
            "Use /customers to select a customer."
        )

        return

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

        logger.error(
            "Owner credit check failed | error=%r",
            e
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

            logger.error(
                "Owner voice credit consumption failed | error=%r",
                e
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

        logger.error(
            "Owner translation credit consumption failed | error=%r",
            e
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

    buttons = []

    for customer in customers:

        customer_id = customer["id"]

        name = (
            customer.get("name")
            or "Customer"
        )

        language = (
            customer.get("language")
            or "Unknown"
        )

        buttons.append([
            {
                "text": f"{name} — {language}",
                "callback_data": f"select_customer:{customer_id}"
            }
        ])

    reply_markup = {
        "inline_keyboard": buttons
    }

    await send_message(
        owner_chat_id,
        "👥 Your Customers\n\nSelect a customer to reply to:",
        reply_markup=reply_markup
    )

def get_owner_session(
    business_id: str,
    owner_chat_id: int
):
    response = (
        supabase
        .table("atupcy_bridge_owner_sessions")
        .select("*")
        .eq("business_id", business_id)
        .eq("owner_chat_id", str(owner_chat_id))
        .limit(1)
        .execute()
    )

    rows = response.data or []

    return rows[0] if rows else None

def set_owner_selected_conversation(
    business_id: str,
    owner_chat_id: int,
    conversation_id: str
):
    existing = get_owner_session(
        business_id=business_id,
        owner_chat_id=owner_chat_id
    )

    data = {
        "business_id": business_id,
        "owner_chat_id": str(owner_chat_id),
        "selected_conversation_id": conversation_id,
        "updated_at": datetime.now(
            timezone.utc
        ).isoformat()
    }

    if existing:

        response = (
            supabase
            .table("atupcy_bridge_owner_sessions")
            .update(data)
            .eq("id", existing["id"])
            .execute()
        )

    else:

        response = (
            supabase
            .table("atupcy_bridge_owner_sessions")
            .insert(data)
            .execute()
        )

    rows = response.data or []

    return rows[0] if rows else None

def clear_owner_selected_conversation(
    business_id: str,
    owner_chat_id: int
):
    existing = get_owner_session(
        business_id=business_id,
        owner_chat_id=owner_chat_id
    )

    if not existing:
        return

    (
        supabase
        .table("atupcy_bridge_owner_sessions")
        .update(
            {
                "selected_conversation_id": None,
                "updated_at": datetime.now(
                    timezone.utc
                ).isoformat()
            }
        )
        .eq(
            "id",
            existing["id"]
        )
        .execute()
    )

def get_owner_selected_conversation(
    business_id: str,
    owner_chat_id: int
):
    owner_session = get_owner_session(
        business_id=business_id,
        owner_chat_id=owner_chat_id
    )

    if not owner_session:
        return None

    conversation_id = owner_session.get(
        "selected_conversation_id"
    )

    if not conversation_id:
        return None

    return get_conversation_by_id(
        conversation_id
    )

async def handle_current_command(
    owner_chat_id: int,
    business: dict
):
    business_id = business["id"]

    conversation = get_owner_selected_conversation(
        business_id=business_id,
        owner_chat_id=owner_chat_id
    )

    if not conversation:

        await send_message(
            owner_chat_id,
            "🎯 No customer is currently selected.\n\n"
            "Use /customers to select a customer."
        )

        return

    customer_id = conversation.get(
        "customer_id"
    )

    if not customer_id:

        await send_message(
            owner_chat_id,
            "I couldn't determine the currently selected customer."
        )

        return

    customer = get_customer_by_id(
        customer_id
    )

    if not customer:

        await send_message(
            owner_chat_id,
            "The currently selected customer could not be found."
        )

        return

    customer_name = (
        customer.get("name")
        or "Customer"
    )

    customer_language = (
        customer.get("language")
        or "Unknown"
    )

    await send_message(
        owner_chat_id,
        f"🎯 Current Customer\n\n"
        f"Customer: {customer_name}\n"
        f"Language: {customer_language}\n\n"
        f"Your messages are currently being sent to this customer."
    )

async def handle_close_command(
    owner_chat_id: int,
    business: dict
):
    business_id = business["id"]

    conversation = get_owner_selected_conversation(
        business_id=business_id,
        owner_chat_id=owner_chat_id
    )

    if not conversation:

        await send_message(
            owner_chat_id,
            "There is no currently selected customer conversation to close."
        )

        return

    conversation_id = conversation["id"]

    customer_id = conversation.get(
        "customer_id"
    )

    customer = None

    if customer_id:
        customer = get_customer_by_id(
            customer_id
        )

    if not customer:

        await send_message(
            owner_chat_id,
            "The customer for this conversation could not be found."
        )

        return

    customer_name = (
        customer.get("name")
        or "Customer"
    )

    customer_chat_id = int(
        customer["telegram_chat_id"]
    )

    customer_language = (
        get_latest_customer_language(
            conversation_id
        )
        or customer.get("language")
        or DEFAULT_OWNER_LANGUAGE
    )


    close_conversation(
        conversation_id
    )


    clear_owner_selected_conversation(
        business_id=business_id,
        owner_chat_id=owner_chat_id
    )


    customer_close_message = (
        "👋 Thank you for contacting us. "
        "This conversation has been closed. "
        "You can send a new message anytime "
        "if you need further assistance."
    )

    try:

        if customer_language.lower() != "english":

            consume_bridge_credits(
                business_id=business_id,
                credits=1,
                conversation_id=conversation_id,
                event_type="translation",
                channel="telegram",
                description="Customer conversation close notification translation"
            )

            translated_close_message = translate(
                text=customer_close_message,
                target_language=customer_language
            )["translated_text"]

        else:

            translated_close_message = (
                customer_close_message
            )

        await send_message(
            customer_chat_id,
            translated_close_message
        )

    except Exception as e:

        logger.error(
            "Customer close message error | error=%r",
            e
        )

        # Fallback message if translation/sending fails

        await send_message(
            customer_chat_id,
            customer_close_message
        )


    await send_message(
        owner_chat_id,
        f"✅ Conversation closed\n\n"
        f"Customer: {customer_name}\n\n"
        f"Use /customers to select another customer."
    )

async def handle_history_command(
    owner_chat_id: int,
    business: dict
):
    business_id = business["id"]

    conversation = get_owner_selected_conversation(
        business_id=business_id,
        owner_chat_id=owner_chat_id
    )

    if not conversation:

        await send_message(
            owner_chat_id,
            "No customer is currently selected.\n\n"
            "Use /customers to select a customer."
        )

        return

    if conversation.get("status") != "active":

        clear_owner_selected_conversation(
            business_id=business_id,
            owner_chat_id=owner_chat_id
        )

        await send_message(
            owner_chat_id,
            "The selected conversation is closed.\n\n"
            "Use /customers to select a customer."
        )

        return

    customer_id = conversation.get(
        "customer_id"
    )

    customer = None

    if customer_id:
        customer = get_customer_by_id(
            customer_id
        )

    customer_name = (
        customer.get("name")
        if customer
        else "Customer"
    )

    messages = get_conversation_messages(
        conversation["id"]
    )

    if not messages:

        await send_message(
            owner_chat_id,
            f"💬 Conversation History\n\n"
            f"Customer: {customer_name}\n\n"
            f"No messages yet."
        )

        return

    lines = [
        "💬 Conversation History",
        "",
        f"Customer: {customer_name}",
        ""
    ]

    for message in messages:

        sender_type = message.get(
            "sender_type"
        )

        translated_text = (
            message.get("translated_text")
            or message.get("original_text")
            or ""
        )

        if not translated_text:
            continue

        if sender_type == "customer":
            label = "Customer"

        elif sender_type == "owner":
            label = "You"

        else:
            label = "Message"

        lines.append(
            f"{label}:\n"
            f"{translated_text}\n"
        )

    history_text = "\n".join(lines)

    await send_message(
        owner_chat_id,
        history_text
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
        response = await request_with_retry(
            client,
            "POST",
            AI_SUPPORT_WEBHOOK_URL,
            json=payload,
            timeout=60
        )

    response.raise_for_status()
    return response.json()

async def transcribe_voice(file_id: str) -> str:

    async with httpx.AsyncClient() as client:

        file_info_response = await request_with_retry(
            client,
            "GET",
            f"{TELEGRAM_API_URL}/getFile",
            params={
                "file_id": file_id
            },
            timeout=30
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
            logger.error(
                "Telegram getFile JSON error | error=%r",
                e
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

        audio_response = await request_with_retry(
            client,
            "GET",
            file_url,
            timeout=60
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

        logger.error(
            "OpenAI transcription error | error=%r",
            e
        )

        raise

    return transcription.text

async def send_message(
    chat_id: int,
    text: str,
    reply_markup: dict | None = None
):

    payload = {
        "chat_id": chat_id,
        "text": text
    }

    if reply_markup:
        payload["reply_markup"] = reply_markup

    async with httpx.AsyncClient() as client:

        response = await request_with_retry(
            client,
            "POST",
            f"{TELEGRAM_API_URL}/sendMessage",
            json=payload,
            timeout=30
        )

        response.raise_for_status()

async def answer_callback_query(
    callback_query_id: str
):
    """
    Tell Telegram that an inline button was clicked.
    This removes the loading/spinner state on the button.
    """

    async with httpx.AsyncClient() as client:

        response = await request_with_retry(
            client,
            "POST",
            f"{TELEGRAM_API_URL}/answerCallbackQuery",
            json={
                "callback_query_id": callback_query_id
            },
            timeout=30
        )

        response.raise_for_status()