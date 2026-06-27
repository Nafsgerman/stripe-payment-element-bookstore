"""Order persistence and the order state machine.

`transition()` is the sole writer of order state (the webhook calls it; the
confirmation page never does) and is forward-only, except that a `failed`
attempt is recoverable; see ALLOWED_TRANSITIONS. Orders are keyed by the
Stripe PaymentIntent id (`pi_...`); `order_ref` is the client idempotency token.
"""
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

from config import config

logger = logging.getLogger("orders")

# --- States ---------------------------------------------------------------
CREATED = "created"
REQUIRES_PAYMENT = "requires_payment"
PROCESSING = "processing"
PAID = "paid"
FAILED = "failed"
REFUNDED = "refunded"  # socket: reachable in the model, no webhook routes here yet

# A failed *attempt* is recoverable: the Payment Element lets a customer retry a
# declined card on the same PaymentIntent, so `failed` must advance to
# paid/processing. Terminal failure is cancellation/expiry, not a single decline.
ALLOWED_TRANSITIONS = {
    CREATED: {REQUIRES_PAYMENT},
    REQUIRES_PAYMENT: {PROCESSING, PAID, FAILED},
    PROCESSING: {PAID, FAILED},
    PAID: {REFUNDED},
    FAILED: {PROCESSING, PAID},
    REFUNDED: set(),
}


@dataclass(frozen=True)
class Order:
    pi_id: str
    order_ref: str
    item: str
    title: str
    amount: int       # integer minor units (cents); never a float
    currency: str
    state: str


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.database_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                pi_id      TEXT PRIMARY KEY,
                order_ref  TEXT NOT NULL UNIQUE,
                item       TEXT NOT NULL,
                title      TEXT NOT NULL,
                amount     INTEGER NOT NULL,
                currency   TEXT NOT NULL,
                state      TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        # Replay guard: event.id is claimed before handling (see claim_event).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_events (
                event_id   TEXT PRIMARY KEY,
                request_id TEXT,
                seen_at    REAL NOT NULL
            )
            """
        )


def _row_to_order(row: sqlite3.Row) -> Order:
    return Order(
        pi_id=row["pi_id"],
        order_ref=row["order_ref"],
        item=row["item"],
        title=row["title"],
        amount=row["amount"],
        currency=row["currency"],
        state=row["state"],
    )


def create_order(
    *, pi_id: str, order_ref: str, item: str, title: str, amount: int, currency: str
) -> Order:
    """Persist a new order at `requires_payment`. Idempotent on `pi_id`."""
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO orders
                (pi_id, order_ref, item, title, amount, currency, state,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (pi_id, order_ref, item, title, amount, currency,
             REQUIRES_PAYMENT, now, now),
        )
    return get_order_by_pi(pi_id)


def get_order_by_pi(pi_id: str) -> Optional[Order]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE pi_id = ?", (pi_id,)
        ).fetchone()
    return _row_to_order(row) if row else None


def get_order_by_ref(order_ref: str) -> Optional[Order]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM orders WHERE order_ref = ?", (order_ref,)
        ).fetchone()
    return _row_to_order(row) if row else None


def claim_event(event_id: str, request_id: str) -> bool:
    """Atomically claim a webhook event. True if claimed, False if already seen.

    PRIMARY KEY + INSERT OR IGNORE makes the claim atomic and replay-safe.
    """
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO processed_events (event_id, request_id, seen_at) "
            "VALUES (?, ?, ?)",
            (event_id, request_id, time.time()),
        )
        return cur.rowcount == 1  # 1 => inserted (claimed); 0 => already present


def transition(pi_id: str, new_state: str, *, request_id: str = "-") -> Order:
    """Move an order forward; no-op if the transition isn't allowed.

    Sole writer of order state. An illegal transition (e.g. a redirect racing
    the webhook) is clamped rather than raised, so a benign out-of-order event
    can't 500. An unknown PaymentIntent is a real anomaly and raises KeyError.
    """
    order = get_order_by_pi(pi_id)
    if order is None:
        raise KeyError(f"Unknown order for PaymentIntent {pi_id}")

    current = order.state
    if new_state == current:
        logger.info(
            "transition no-op · request_id=%s · pi=%s · already=%s",
            request_id, pi_id, current,
        )
        return order

    if new_state not in ALLOWED_TRANSITIONS.get(current, set()):
        logger.warning(
            "transition rejected · request_id=%s · pi=%s · %s->%s (not allowed)",
            request_id, pi_id, current, new_state,
        )
        return order

    with _connect() as conn:
        conn.execute(
            "UPDATE orders SET state = ?, updated_at = ? WHERE pi_id = ?",
            (new_state, time.time(), pi_id),
        )
    logger.info(
        "transition · request_id=%s · pi=%s · %s->%s",
        request_id, pi_id, current, new_state,
    )
    return get_order_by_pi(pi_id)
