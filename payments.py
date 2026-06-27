"""Stripe boundary + the catalog that is the server-side price source of truth.

Everything that talks to Stripe lives here, behind a small surface
(`create_intent`, `retrieve_intent`, and later `verify_webhook`). The web layer
never imports `stripe` directly, which keeps the SDK swappable and testable and
keeps `app.py` to routes.

Two security invariants enforced here:
  * Amounts come from THIS catalog, never from the client. A client posting its
    own price is the most basic payments vuln.
  * Money is integer minor units (cents) everywhere. Never a float.
"""
from dataclasses import dataclass

import stripe

from config import config

stripe.api_key = config.stripe_secret_key

CURRENCY = "usd"


@dataclass(frozen=True)
class Book:
    id: str
    title: str
    amount: int  # cents


# Mirrors the boilerplate's hardcoded case statement, centralised as the price
# source of truth. In production this is a catalog table / pricing service.
CATALOG = {
    "1": Book("1", "The Art of Doing Science and Engineering", 2300),
    "2": Book("2", "The Making of Prince of Persia: Journals 1985-1993", 2500),
    "3": Book("3", "Working in Public: The Making and Maintenance of Open Source", 2800),
}


class UnknownItem(Exception):
    """Raised when a requested item id isn't in the catalog."""


def get_book(item_id: str) -> Book:
    book = CATALOG.get(item_id)
    if book is None:
        raise UnknownItem(item_id)
    return book


def create_intent(order_ref: str, item_id: str) -> stripe.PaymentIntent:
    """Create one PaymentIntent for a logical order.

    `order_ref` is passed as the Stripe idempotency key: a retried request
    (dropped network, double click) returns the *same* PaymentIntent instead of
    creating a duplicate charge. The amount is looked up server-side from the
    catalog and never read from the request body.
    """
    book = get_book(item_id)
    return stripe.PaymentIntent.create(
        amount=book.amount,
        currency=CURRENCY,
        # Enable/disable methods from the Dashboard with zero code change.
        automatic_payment_methods={"enabled": True},
        metadata={"order_ref": order_ref, "item": item_id},
        idempotency_key=order_ref,
    )


def retrieve_intent(pi_id: str) -> stripe.PaymentIntent:
    """Fetch authoritative PaymentIntent state from Stripe.

    Used by the success page as a fallback for the redirect-beats-webhook race
    (and, until the webhook lands, as the primary status read).
    """
    return stripe.PaymentIntent.retrieve(pi_id)
