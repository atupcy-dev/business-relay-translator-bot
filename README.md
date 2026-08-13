# Business Relay Translator Bot — Setup

A two-party translation relay: sits between a business owner and their
customers, translating both directions live. The owner registers once;
any other person who messages becomes a customer whose messages get
translated and forwarded to the owner.

## 1. Create a new Telegram bot

Same as before — message **@BotFather**, `/newbot`, get a token.
(A separate bot from your personal translator, since this one behaves
very differently.)

## 2. Reuse your existing OpenAI and Supabase credentials

No need for new accounts — same OpenAI key and Supabase project as
your translator bot work fine here, just new tables.

## 3. Create the Supabase tables

Run this in the SQL Editor:

```sql
-- Stores the single registered business owner for this bot
create table relay_config (
    id bigint primary key,
    owner_chat_id bigint,
    active_customer_chat_id bigint
);

-- Maps customers to short numbers per owner (e.g. "Customer 3")
create table relay_customers (
    id bigint generated always as identity primary key,
    owner_chat_id bigint not null,
    customer_chat_id bigint not null,
    customer_number int not null,
    created_at timestamptz default now()
);

-- Tracks each customer's most recently detected language, so owner
-- replies get translated back into the right language
create table relay_messages (
    id bigint generated always as identity primary key,
    customer_chat_id bigint not null,
    customer_language text not null,
    created_at timestamptz default now()
);

create policy "Allow all for relay_config" on relay_config for all to anon using (true) with check (true);
create policy "Allow all for relay_customers" on relay_customers for all to anon using (true) with check (true);
create policy "Allow all for relay_messages" on relay_messages for all to anon using (true) with check (true);
```

## 4. Configure and run

```bash
python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload --port 8002
```

(Using port 8002 so it can run alongside your other bots without a
port clash.)

## 5. Expose and connect

```bash
ngrok http 8002
```

```bash
curl.exe -F "url=https://YOUR-NGROK-URL.ngrok-free.dev/webhook" https://api.telegram.org/botYOUR_BOT_TOKEN/setWebhook
```

## 6. Test it

You'll need two Telegram accounts (or ask a friend) to test both
sides:

1. **As the business owner**: message the bot with `/register`
2. **As a customer** (different account): send a message in any
   language — e.g. "Mo fẹ́ ra àwọn ẹ̀wù" (Yoruba)
3. **Back as the owner**: you should receive the translated message,
   tagged "Customer 1"
4. Send `/switch 1` to start replying to them
5. Send a normal message (in English) — the customer should receive
   it translated into the language they wrote in

## Known limitations (MVP)

- **One active customer at a time** — the owner must `/switch` between
  customers manually. A future version could auto-detect via
  Telegram's reply-to-message feature instead.
- **Single business per bot** — this bot instance supports exactly one
  registered owner. Multi-business support would need a different
  registration model.
- **Text and voice input supported; voice replies not yet added** to
  this mode (the personal translator bot has TTS replies — porting
  that here is a natural next step).