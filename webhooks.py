"""Stripe webhook handling: signature verification + idempotent dispatch.

construct_and_dispatch() is called by app.py's /webhook route and returns
(http_status, message), returned verbatim. This module is the sole writer of
order state; the confirmation page only reads.
"""
import logging

import stripe

import orders

logger = logging.getLogger("webhooks")

# stripe-python exposes this at the top level in v8+; fall back for older SDKs.
try:
    SignatureVerificationError = stripe.SignatureVerificationError
except AttributeError:  # pragma: no cover - older SDKs
    SignatureVerificationError = stripe.error.SignatureVerificationError


def _pi_id(event) -> str:
    return event["data"]["object"]["id"]


def _on_succeeded(event, request_id: str) -> None:
    orders.transition(_pi_id(event), orders.PAID, request_id=request_id)


def _on_failed(event, request_id: str) -> None:
    orders.transition(_pi_id(event), orders.FAILED, request_id=request_id)


def _on_processing(event, request_id: str) -> None:
    orders.transition(_pi_id(event), orders.PROCESSING, request_id=request_id)


# event.type -> handler. Anything not here is acknowledged (200) and ignored.
HANDLERS = {
    "payment_intent.succeeded": _on_succeeded,
    "payment_intent.payment_failed": _on_failed,
    "payment_intent.processing": _on_processing,
}


def construct_and_dispatch(
    payload: bytes,
    sig_header: str,
    webhook_secret: str,
    request_id: str,
) -> tuple[int, str]:
    # Verify the signature on the RAW body before parsing any JSON.
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except ValueError:
        logger.warning("webhook bad payload · request_id=%s", request_id)
        return 400, "invalid payload"
    except SignatureVerificationError:
        logger.warning("webhook bad signature · request_id=%s", request_id)
        return 400, "invalid signature"

    event_id = event["id"]
    event_type = event["type"]

    # Replay guard: claim the event before any work.
    if not orders.claim_event(event_id, request_id):
        logger.info(
            "webhook duplicate ignored · request_id=%s · event=%s · type=%s",
            request_id, event_id, event_type,
        )
        return 200, "duplicate ignored"

    handler = HANDLERS.get(event_type)
    if handler is None:
        logger.info(
            "webhook unhandled type ack'd · request_id=%s · type=%s",
            request_id, event_type,
        )
        return 200, "ignored"

    # An event for a PaymentIntent we never recorded is an anomaly, not a
    # transient error: ack it so Stripe stops retrying.
    try:
        handler(event, request_id)
    except KeyError:
        logger.warning(
            "webhook no matching order · request_id=%s · type=%s · pi=%s",
            request_id, event_type, _pi_id(event),
        )
        return 200, "no matching order"

    return 200, "ok"
