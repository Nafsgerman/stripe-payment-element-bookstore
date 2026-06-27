# Stripe Book Store: Payment Element Integration

A small Flask bookstore that takes real card payments through the **Stripe Payment Element**, fulfills each order from a **verified webhook**, and shows a confirmation page with the amount charged and the PaymentIntent ID.

**At a glance**
- Embedded payment flow via the Stripe **Payment Element** (not Checkout)
- **PaymentIntent-driven**, with the full payment lifecycle owned server-side
- **Webhook-authoritative fulfillment**: the redirect only displays state, never writes it
- **Server-side pricing** in integer minor units; the client never sets the amount
- **Idempotent** intent creation, with webhook replay protection
- An explicit **order state machine**, structured so new Stripe features drop in cleanly

The starting point was Stripe's boilerplate (`marko-stripe/sa-takehome-project-python`): a Flask app with a static book list and a checkout page, and no payment logic. This integration adds the PaymentIntent lifecycle, a webhook that is the single source of truth for fulfillment, and a small persisted order state machine so that "the order is paid" is a fact the server owns, not a claim the browser makes on redirect.

I kept the project on Flask and made minimal structural changes to the boilerplate, because the assignment was to integrate payments, not to re-platform. Where I added structure, a small database and separate modules, it was deliberate, and I explain each choice below.

---

## Quickstart

Tested from a clean clone. Commands are macOS / zsh.

### 1. Prerequisites
- Python 3.11+
- A Stripe account (test mode) and the [Stripe CLI](https://docs.stripe.com/stripe-cli)

### 2. Get the code and install
From a clone, or by unzipping the submitted archive:
```
# git clone <your-repo-url>   # if you received a repo
cd psa-takehome-python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure
```
cp .env.example .env
```
Open `.env` and fill in your test keys from the [Stripe Dashboard](https://dashboard.stripe.com/test/apikeys):

| Variable | Where it comes from |
|---|---|
| `STRIPE_SECRET_KEY` | Dashboard → Developers → API keys (`sk_test_...`) |
| `STRIPE_PUBLISHABLE_KEY` | Same page (`pk_test_...`) |
| `STRIPE_WEBHOOK_SECRET` | Printed by `stripe listen` in the next step (`whsec_...`) |
| `DOMAIN` | `http://localhost:4242`, used to build the PaymentIntent return URL |
| `DATABASE_PATH` | `orders.db` (default is fine) |

### 4. Start the webhook listener
In a second terminal, forward Stripe events to the local app and copy the signing secret it prints into `STRIPE_WEBHOOK_SECRET`:
```
stripe listen --forward-to localhost:4242/webhook
```

### 5. Run the app
```
python3 app.py
```
Open **http://localhost:4242**. The app binds port 4242 in `app.py` (Stripe's sample-app convention, and it sidesteps the macOS AirPlay conflict below); `DOMAIN` in `.env` must match so the redirect returns to the right local URL.

> **Why port 4242 and not 5000?** On macOS, the AirPlay Receiver service binds port 5000 by default, so a Flask app there appears to "work" but is shadowed by AirTunes. Running on 4242 sidesteps it. See [Environment notes](#environment-notes).

### 6. Try it with test cards
Any future expiry, any CVC, any postal code.

| Scenario | Card number | Expected result |
|---|---|---|
| Success | `4242 4242 4242 4242` | Redirects to confirmation; order → `paid` |
| Decline | `4000 0000 0000 0002` | Inline decline message, can retry; order → `failed` |
| 3DS / SCA required | `4000 0025 0000 3155` | Authentication challenge, then success; order → `paid` |

To see the failed-attempt recovery path: enter the decline card, then on the same page retry with `4242 4242 4242 4242`. The same PaymentIntent succeeds and the order advances `failed → paid`.

### 7. Run the tests
```
python3 -m pytest -q
```

---

## How the solution works

### Request flow

```
 Browser                         Flask app                    Stripe
   │                                 │                            │
   │  GET /checkout?item=<id>        │                            │
   │────────────────────────────────▶  renders checkout page     │
   │                                 │                            │
   │  POST /create-payment-intent    │                            │
   │  { item, order_ref }            │                            │
   │────────────────────────────────▶  create_intent(order_ref)  │
   │                                 │───────────────────────────▶│  PaymentIntent.create
   │                                 │   persist order @           │  (amount from catalog,
   │                                 │   requires_payment          │   idempotency_key=order_ref)
   │   client_secret                 │◀───────────────────────────│
   │◀────────────────────────────────│                            │
   │                                 │                            │
   │  Payment Element.confirmPayment │                            │
   │────────────────────────────────────────────────────────────▶│  (card + any 3DS challenge)
   │                                 │                            │
   │   redirect to return_url        │        payment_intent.succeeded (webhook)
   │◀────────────────────────────────────────────│  ◀─────────────│
   │                                 │   verify signature,        │
   │  GET /success?payment_intent=…  │   transition → paid        │
   │────────────────────────────────▶   read order state (display)│
   │   confirmation: total + pi_…    │                            │
   │◀────────────────────────────────│                            │
```

Two paths reach the server after a payment: the **browser redirect** (fast, client-driven, display-only) and the **webhook** (authoritative, Stripe-driven, the only thing that writes "paid"). Keeping those separate is the core design idea; see [Source of truth](#source-of-truth-webhook-not-redirect).

### Architecture

The web layer stays thin and every Stripe concern sits behind a module boundary, so the integration is swappable and testable, and so a new feature can be added in one place during the live round.

| File | Responsibility |
|---|---|
| `app.py` | Routes only: `/`, `/checkout`, `/create-payment-intent`, `/success`, `/webhook`. No Stripe SDK calls, no SQL. |
| `payments.py` | The Stripe boundary. Creates and retrieves PaymentIntents; owns the catalog that is the server-side price source of truth. Nothing else imports `stripe`. |
| `orders.py` | SQLite persistence and a forward-only order state machine. `transition()` is the sole writer of order state. Includes the webhook replay guard. |
| `webhooks.py` | Inbound event handling: signature verification on the raw body, then a `HANDLERS` dispatch table mapping event type → state transition. |
| `config.py` | Typed, immutable config loaded once from the environment. |
| `public/js/checkout.js` | Mounts the Payment Element, generates one `order_ref` per page load, calls `confirmPayment` with the return URL. |

### Stripe APIs used

- **PaymentIntents API**: models the full lifecycle of a single payment (`requires_payment_method → requires_action → processing → succeeded`). Chosen over the legacy Charges API because it handles SCA/3DS natively and gives one object to track per order.
- **Payment Element + Stripe.js**: the embedded, multi-method UI. Chosen over Stripe Checkout because the exercise requires it and because embedding keeps the full PaymentIntent lifecycle, confirmation, and return flow under the application's control. The client confirms the PaymentIntent with the `client_secret`; the server owns the intent's creation and amount.
- **Webhooks** (`construct_event`): Stripe's server-to-server notification of the real outcome. This is what fulfills the order, not the redirect.

---

## Design decisions

### Payment Element, not Checkout

Stripe Checkout is a hosted page: you hand off to Stripe and get a result back. The Payment Element is embedded, which means *I* own the PaymentIntent's creation, its amount, its confirmation, and the return flow. That ownership is the entire point of the exercise, and it is also the new surface for me: my prior Stripe work used a Checkout Session, so wiring the `client_secret` flow, `confirmPayment`, and the redirect/return handling directly was the part I worked through here. The trade-off is honest: Checkout would have been less code, but it would have hidden exactly the lifecycle this app needs to demonstrate.

Methods are enabled with `automatic_payment_methods: { enabled: true }`, so cards today and wallets or local methods later are a Dashboard toggle, not a code change.

### I added persistence to a DB-free boilerplate, on purpose

The boilerplate has no database; it reads the item from a GET parameter. That is fine for displaying a price and insufficient for *fulfilling an order*. To make the webhook the real source of truth, rather than something I merely describe, order state has to live somewhere the webhook can write and the confirmation page can read. So I added a single SQLite `orders` table keyed by the PaymentIntent id (`pi_...`).

This is deliberately minimal. It is enough to make the state machine, the webhook-as-truth pattern, and the redirect race *real and testable*. It is not a production datastore; [`LIMITATIONS.md`](LIMITATIONS.md) and [`RECOMMENDATIONS.md`](RECOMMENDATIONS.md) cover the path to Postgres.

### Source of truth: webhook, not redirect

After a successful payment Stripe redirects the browser to the return URL. It is tempting to mark the order paid there, and wrong. The redirect is a client telling you it succeeded; it can be lost, replayed, or beaten to the server by the webhook. So fulfillment writes happen in exactly one place: the `payment_intent.succeeded` webhook handler, which calls `transition(pi_id, "paid")`. The `/success` page only *reads* order state for display. The rule the code follows: **display may lead the database; truth never does.**

That leaves one real race: the redirect can arrive before the webhook, so the success page might read an order still at `requires_payment`. The page resolves this with a fallback, a live `PaymentIntent.retrieve` to show the customer the accurate status, while still never writing fulfillment from the request path. The webhook lands a moment later and writes the truth. The state machine is **forward-only**, so a late or duplicate event can never move an order backwards: a `requires_payment` write arriving after `paid` is clamped, not applied.

### Money is server-derived, in integer minor units

Amounts come from the catalog in `payments.py`, never from the client. A client that can post its own price is the most basic payments vulnerability, and this app does not have it. Money is integer cents end to end (`2300`, `2500`, `2800`): never a float.

### Idempotency

The client generates one `order_ref` (a UUID) per page load and sends it with the create-intent request. That `order_ref` is passed to Stripe as the **idempotency key**, so a double-click or a retried request after a dropped connection returns the *same* PaymentIntent instead of creating a duplicate charge. On a page refresh the server also looks up the existing order by `order_ref` and returns its intent rather than minting a new one, so neither layer double-charges. `order_ref` is the unique key on the order row.

### One order state machine

```
created → requires_payment → processing → paid → refunded
                         └──→ failed ──(retry)──→ processing / paid
```

`created` is conceptual (no row yet, `pi_id` is the primary key, so the first row is written at `requires_payment`). Every later move goes through `transition()`. The graph is **forward-only with one deliberate exception**: `failed` is recoverable, because the Payment Element lets a customer retry a declined card on the *same* PaymentIntent and that retry can succeed, so a later `succeeded` event must advance the order rather than be stranded (see [Challenges](#how-i-approached-this-docs-and-challenges)). Terminal failure comes from cancellation, not a single decline. `refunded` is defined in the model but intentionally has no webhook routed to it yet, it is a labeled socket for the refund feature, the natural thing to add in the live extension round.

---

## Production concerns

This submission keeps a minimal surface and spends its rigor on what actually breaks in production:

- **Idempotency**: one PaymentIntent per logical order, keyed so retries never double-charge.
- **Webhook as source of truth**: fulfillment is driven by verified Stripe events, not by the browser.
- **Replay protection**: each `event.id` is claimed in a `processed_events` table before handling, so Stripe redeliveries don't double-apply the same event. Handlers are independently idempotent as well, because the state machine no-ops a repeated target. The claim and the state write are separate operations here, so in production I'd make them one transactional/outbox-style unit to also cover a crash between claim and transition; see [`RECOMMENDATIONS.md`](RECOMMENDATIONS.md).
- **SCA / PSD2**: the Payment Element handles 3DS challenges natively, which matters directly for European cards. The `4000 0025 0000 3155` test card above exercises the full `requires_action → succeeded` path.
- **Signature verification**: every webhook is verified against the raw request body with `construct_event` before any JSON is parsed; a bad signature is rejected with `400`.
- **Observability**: every state transition logs `request_id · pi_id · old→new`, so an order's history is reconstructable from logs.

---

## How I approached this, docs, and challenges

I started by separating *display truth* from *fulfillment truth*, because that distinction decides the whole architecture. Once the webhook is the only writer, everything else (the state machine, the SQLite table, the success-page fallback) follows from it. I built the happy path end to end first, then added persistence and the state machine, then the webhook and the race handling, then decline and 3DS, then tests and this document.

**Which docs I used to complete the project:**
- [Accept a payment with the Payment Element](https://docs.stripe.com/payments/payment-element): the embedded integration and `confirmPayment` / `return_url` contract.
- [PaymentIntents lifecycle](https://docs.stripe.com/payments/paymentintents/lifecycle): the status model the state machine mirrors.
- [Build a webhook](https://docs.stripe.com/webhooks) and [Handling payment events](https://docs.stripe.com/payments/handling-payment-events): signature verification and fulfilling on `payment_intent.succeeded`.
- [Stripe CLI](https://docs.stripe.com/stripe-cli): local event forwarding and the signing secret.

**Challenges:**
- *Payment Element vs Checkout.* This was the genuinely new surface versus my prior Checkout work, owning the `client_secret` flow and the return handling rather than handing off to a hosted page.
- *The redirect-vs-webhook race.* Naming it precisely and resolving it with a forward-only state machine plus a display-only retrieve fallback, instead of papering over it.
- *Modeling a failed attempt as a terminal state.* I first treated `payment_intent.payment_failed` as a terminal `failed` order. Live testing surfaced the flaw: the Payment Element lets a customer retry a declined card on the *same* PaymentIntent, and that retry can succeed, Stripe charges the card while the order would have stayed stuck at `failed`, a silent fulfillment miss. I had conflated an *attempt* outcome with an *order* outcome. The fix made `failed` recoverable (`failed → processing/paid`), so the later `succeeded` webhook advances the order correctly; terminal failure now comes from cancellation, not a single decline. I verified the corrected path end to end: decline, retry, and a webhook-driven `failed → paid` transition with the order row matching the charge.
- *Environment.* Two things cost real time and are worth flagging for whoever runs this next; see below.

---

## Environment notes

- **macOS AirPlay on port 5000.** macOS runs an AirPlay Receiver that binds `:5000`. A Flask app started there looks alive but is shadowed, `curl -i localhost:5000` returns a `Server: AirTunes/...` header, which is how I diagnosed it. This app runs on **4242**, and `DOMAIN` in `.env` must match so the return URL is correct.
- **Safari and the 3DS frame.** The 3DS challenge for `4000 0025 0000 3155` renders and completes in Chrome. In Safari the challenge frame can blank out under default privacy settings. If you are reviewing the SCA path, use Chrome.

---

## Extending this for a more robust instance

The structure is built so each of these lands in one place:

- **More payment methods**: already wired via `automatic_payment_methods`; enable wallets or local methods from the Dashboard with no code change.
- **Refunds**: route a `charge.refunded` (or `payment_intent` refund) event to the `refunded` socket already defined in the state machine, and add one entry to the `HANDLERS` table. This is the natural live-extension feature.
- **Saved cards / repeat buyers**: attach a Stripe Customer to the PaymentIntent and store the customer id alongside the order.
- **Async payment methods**: the `processing` state and its handler already exist for methods that settle asynchronously; the success page would poll or rely on the webhook.

The full production roadmap, mapped from each current limitation, is in [`RECOMMENDATIONS.md`](RECOMMENDATIONS.md); what is deliberately *not* built, and why, is in [`LIMITATIONS.md`](LIMITATIONS.md).

---

## Tests

`python3 -m pytest -q` runs the suite in `tests/test_payments.py`, focused on the logic that matters rather than coverage for its own sake:

- **Amount calculation**: the catalog is the price source of truth; unknown items are rejected; `create_intent` sends the server-side amount and the idempotency key, never client input.
- **Webhook signature verification**: a valid signature is accepted and writes the order; a bad signature or wrong secret is rejected with `400`; a valid signature over malformed JSON is a `400` payload error.
- **State transitions**: the happy path, the redirect-race clamp (a backward move is a no-op), a failed attempt recovering to `paid` on retry, the terminal `refunded` state, and an unknown PaymentIntent.
- **Idempotency**: duplicate order creation is ignored, and the event replay guard admits each `event.id` exactly once.

---

## Closing

This submission deliberately prioritizes payment correctness over storefront breadth. The effort went into the properties that make a payments integration reliable: server-authoritative pricing, webhook-driven fulfillment, idempotency, replay safety, and an explicit order state machine. Storefront breadth was a deliberate non-goal. [`LIMITATIONS.md`](LIMITATIONS.md) and [`RECOMMENDATIONS.md`](RECOMMENDATIONS.md) describe how the same architecture would evolve into a production-ready integration.
