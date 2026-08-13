# Business Relay Translator Bot

A two-party AI translation relay that lets a business owner and a
customer who don't share a language communicate naturally — the
business owner never has to leave their own language.

Built by **Oladejo Khadijat** ([Atupcy LTD](https://github.com)) as a
follow-up to the personal Translator Bot, solving a real gap: most
translation tools serve one person, but real businesses need
translation *between* two people.

## The problem this solves

A customer messages a business in Yoruba. The business owner only
reads English. Most translation bots can't help here — they translate
*for* one user, not *between* two. This agent sits in the middle,
translating both directions live, so the business owner replies
normally in their own language and the customer receives it in
theirs.

## What it does

- **Two-party live translation relay** — not a personal translation
  tool, but an agent that sits between a business and its customers
- **Automatic customer tracking** — every new customer who messages
  gets a short reference number (e.g. "Customer 3"), so the business
  owner always knows who they're talking to
- **Simple routing** — the business owner switches between active
  customer conversations with one command
- **Voice input supported** — customers can send voice notes;
  transcribed and translated automatically
- **Built on the same translation core** as the personal Translator
  Bot (GPT-4o detection + translation), proving the underlying engine
  is modular and reusable across products

## Who this is for

Any business that talks to customers, suppliers, or partners in a
language its team doesn't speak:

- E-commerce brands selling to international customers
- Import/export and trading businesses
- Travel and hospitality teams
- Recruitment agencies screening international candidates
- Any small business currently losing deals to a language gap

## Architecture

```
Customer message (any language)
        |
        v
Telegram webhook (FastAPI)
        |
        v
GPT-4o: detect language + translate to owner's language
        |
        v
Forwarded to business owner, tagged "Customer N"
        |
   (owner replies, /switch <N> to target the right customer)
        |
        v
GPT-4o: translate owner's reply into that customer's language
        |
        v
Sent back to the customer
```

## Tech stack

FastAPI · OpenAI GPT-4o · OpenAI Whisper · Supabase (Postgres) ·
Telegram Bot API

## Current limitations (MVP, documented honestly)

- One active customer at a time — the owner manually switches with
  `/switch <number>` rather than the bot auto-detecting via
  reply-threads
- One business per bot instance — no multi-tenant support yet
- Voice **replies** from the owner aren't synthesized yet (the
  personal Translator Bot already has this via TTS — porting it here
  is a natural next step)
- Currently on Telegram; WhatsApp is the natural next platform given
  where target businesses actually operate

## Roadmap

This is designed to grow into a full AI customer support agent —
the translation layer here becomes one module inside a larger system
that also answers questions, takes orders, and handles support
end-to-end, still in the customer's own language.