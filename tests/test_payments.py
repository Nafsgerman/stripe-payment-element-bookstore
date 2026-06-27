"""Tests for the logic that actually breaks in production.

Scope is deliberate, not coverage theater: amount calculation, idempotent
PaymentIntent creation, the forward-only order state machine, and webhook
signature verification + idempotent dispatch. Each test asserts a behavior a
reviewer could probe in the live round.

Stripe network calls are stubbed only where they'd leave the process
(`PaymentIntent.create`). Webhook tests build a REAL `Stripe-Signature` header
and run it through the real `construct_event`, so signature verification is
genuinely exercised rather than mocked away.
"""
import hashlib
import hmac
import json
import time

import pytest
import stripe

import orders
import payments
import webhooks
from config import config


# --- helpers --------------------------------------------------------------

def _sign(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Build a valid Stripe-Signature header (t=...,v1=HMAC-SHA256) for `payload`."""
    ts = timestamp or int(time.time())
    signed = f"{ts}.{payload.decode()}".encode()
    mac = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def _event(pi_id: str, event_type: str, event_id: str = "evt_test") -> bytes:
    payload = {
        "id": event_id,
        "object": "event",
        "type": event_type,
        "data": {
            "object": {
                "id": pi_id,
                "object": "payment_intent",
            }
        },
    }
    return json.dumps(payload).encode()


def _seed(pi_id="pi_test", order_ref="ref_test", item="1",
          title="Test Book", amount=2300, currency="usd"):
    return orders.create_order(pi_id=pi_id, order_ref=order_ref, item=item,
                               title=title, amount=amount, currency=currency)


# --- amount calculation: server is the price source of truth --------------

@pytest.mark.parametrize("item_id, expected", [("1", 2300), ("2", 2500), ("3", 2800)])
def test_catalog_amount_is_server_truth(item_id, expected):
    assert payments.get_book(item_id).amount == expected


def test_unknown_item_is_rejected():
    with pytest.raises(payments.UnknownItem):
        payments.get_book("999")


def test_create_intent_uses_catalog_amount_and_idempotency_key(monkeypatch):
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return {"id": "pi_123", "client_secret": "cs_123"}

    monkeypatch.setattr(stripe.PaymentIntent, "create", fake_create)

    payments.create_intent("ref-abc", "2")

    # Amount is derived server-side from the catalog, never read from the client.
    assert captured["amount"] == 2500
    assert captured["currency"] == "usd"
    # order_ref doubles as the Stripe idempotency key, so a retried request
    # (dropped network, double click) reuses one intent instead of duplicating.
    assert captured["idempotency_key"] == "ref-abc"
    assert captured["automatic_payment_methods"] == {"enabled": True}
    assert captured["metadata"] == {"order_ref": "ref-abc", "item": "2"}


# --- create_order idempotency ---------------------------------------------

def test_create_order_persists_at_requires_payment():
    assert _seed().state == orders.REQUIRES_PAYMENT


def test_create_order_is_idempotent_on_pi_id():
    _seed(pi_id="pi_dup", title="First", amount=2300)
    again = _seed(pi_id="pi_dup", title="Second", amount=9999)  # same PI, new data
    assert again.title == "First" and again.amount == 2300      # original untouched


# --- forward-only state machine -------------------------------------------

def test_happy_path_transition():
    _seed(pi_id="pi_ok")
    assert orders.transition("pi_ok", orders.PAID).state == orders.PAID


def test_redirect_race_is_clamped_not_reversed():
    # Webhook already wrote PAID; a late/duplicate event tries to move it
    # backward. Clamp instead of error so the race resolves cleanly.
    _seed(pi_id="pi_race")
    orders.transition("pi_race", orders.PAID)
    assert orders.transition("pi_race", orders.REQUIRES_PAYMENT).state == orders.PAID


def test_same_state_transition_is_noop():
    _seed(pi_id="pi_same")
    orders.transition("pi_same", orders.PAID)
    assert orders.transition("pi_same", orders.PAID).state == orders.PAID


def test_failed_attempt_can_recover_to_paid():
    # A declined card can be retried on the SAME PaymentIntent and succeed.
    # `failed` is an attempt outcome, not a terminal order state, so a later
    # `succeeded` webhook must advance the order rather than be clamped.
    _seed(pi_id="pi_retry")
    orders.transition("pi_retry", orders.FAILED)
    assert orders.transition("pi_retry", orders.PAID).state == orders.PAID


def test_refunded_is_terminal():
    _seed(pi_id="pi_term")
    orders.transition("pi_term", orders.PAID)
    orders.transition("pi_term", orders.REFUNDED)
    assert orders.transition("pi_term", orders.PAID).state == orders.REFUNDED


def test_refunded_socket_reachable_from_paid():
    _seed(pi_id="pi_ref")
    orders.transition("pi_ref", orders.PAID)
    assert orders.transition("pi_ref", orders.REFUNDED).state == orders.REFUNDED


def test_transition_on_unknown_payment_intent_raises():
    with pytest.raises(KeyError):
        orders.transition("pi_nonexistent", orders.PAID)


# --- webhook replay guard --------------------------------------------------

def test_claim_event_has_a_single_winner():
    assert orders.claim_event("evt_once", "req") is True
    assert orders.claim_event("evt_once", "req") is False


# --- webhook signature verification + dispatch ----------------------------

def test_valid_signature_succeeded_event_writes_paid():
    _seed(pi_id="pi_hook")
    body = _event("pi_hook", "payment_intent.succeeded", "evt_paid")
    status, msg = webhooks.construct_and_dispatch(
        body, _sign(body, config.stripe_webhook_secret),
        config.stripe_webhook_secret, "req-1")
    assert (status, msg) == (200, "ok")
    assert orders.get_order_by_pi("pi_hook").state == orders.PAID


def test_duplicate_event_is_ignored():
    _seed(pi_id="pi_dupe")
    body = _event("pi_dupe", "payment_intent.succeeded", "evt_dupe")
    sig = _sign(body, config.stripe_webhook_secret)
    webhooks.construct_and_dispatch(body, sig, config.stripe_webhook_secret, "req-1")
    status, msg = webhooks.construct_and_dispatch(
        body, sig, config.stripe_webhook_secret, "req-2")
    assert (status, msg) == (200, "duplicate ignored")


def test_bad_signature_is_rejected():
    _seed(pi_id="pi_badsig")
    body = _event("pi_badsig", "payment_intent.succeeded")
    status, msg = webhooks.construct_and_dispatch(
        body, "t=1,v1=deadbeef", config.stripe_webhook_secret, "req")
    assert (status, msg) == (400, "invalid signature")


def test_signature_from_wrong_secret_is_rejected():
    body = _event("pi_x", "payment_intent.succeeded")
    status, msg = webhooks.construct_and_dispatch(
        body, _sign(body, "whsec_wrong_secret"), config.stripe_webhook_secret, "req")
    assert (status, msg) == (400, "invalid signature")


def test_valid_signature_but_malformed_json_is_bad_payload():
    body = b"not-json"
    status, msg = webhooks.construct_and_dispatch(
        body, _sign(body, config.stripe_webhook_secret),
        config.stripe_webhook_secret, "req")
    assert (status, msg) == (400, "invalid payload")


def test_event_for_unknown_order_is_acked():
    # No row for this PI: an anomaly, not a transient error. Ack so Stripe stops
    # retrying; the order state machine is never moved.
    body = _event("pi_ghost", "payment_intent.succeeded", "evt_ghost")
    status, msg = webhooks.construct_and_dispatch(
        body, _sign(body, config.stripe_webhook_secret),
        config.stripe_webhook_secret, "req")
    assert (status, msg) == (200, "no matching order")


def test_unhandled_event_type_is_acked():
    body = _event("pi_any", "charge.refunded", "evt_unhandled")
    status, msg = webhooks.construct_and_dispatch(
        body, _sign(body, config.stripe_webhook_secret),
        config.stripe_webhook_secret, "req")
    assert (status, msg) == (200, "ignored")
