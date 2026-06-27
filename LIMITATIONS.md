# Limitations

What this submission deliberately does *not* do, and why. Each item is a scoping decision, not an oversight; the production fix for each is in [`RECOMMENDATIONS.md`](RECOMMENDATIONS.md).

The guiding rule was to keep a minimal surface and spend the effort on correctness where payments actually break, not to gold-plate features that add lines without adding rigor.

### Persistence is single-file SQLite
The order store is one SQLite file keyed by PaymentIntent id. That is enough to make the state machine, webhook-as-source-of-truth, and the redirect race *real and testable*, but it is not built for concurrency or scale. Concurrent webhook deliveries serialize on a single file, and there is no connection pooling, replication, or migration tooling.

### Single currency, no tax or shipping
Amounts are integer USD cents from a fixed catalog. There is no tax calculation, no shipping, and no multi-currency handling. The money path is correct (server-derived, integer minor units), but the commerce model is intentionally trivial.

### Fulfillment writes to the order table only
The `payment_intent.succeeded` handler advances the order state and stops there. There is no downstream fulfillment: no receipt email, no inventory decrement, no order-management handoff. The webhook is wired as the *place* fulfillment belongs, but the only side effect today is the state transition.

### The catalog is a fixed dictionary
The server is the price source of truth, but the catalog itself is a hardcoded mapping of three books, mirroring the boilerplate's case statement. There is no product service, no admin, and no dynamic pricing.

### Terminal failure is modeled narrowly
A failed payment *attempt* is recoverable: a declined card can be retried on the same PaymentIntent and advance `failed → paid`. True terminal failure (PaymentIntent canceled or expired) is not yet routed to a distinct terminal state; there is no `canceled` state and no handler for `payment_intent.canceled`. In practice a stuck order would sit at `failed` or `processing` rather than being explicitly closed.

### No retry/backoff beyond SDK defaults
Outbound Stripe calls rely on the SDK's built-in retry behavior. There is no application-level backoff, circuit breaking, or dead-letter handling for transient Stripe or network failures, and no reconciliation job for orders stuck in a non-terminal state.

### Refunds are modeled but not handled
`refunded` exists in the state machine as a reachable state with no webhook routed to it. It is a deliberate socket for the live-extension round, not a working feature; issuing or recording a refund is not implemented.

### Tests cover core logic, not the full surface
The suite targets the logic that breaks in production: amount calculation, idempotent intent creation, signature verification, state transitions, and replay safety. It is not end-to-end: there are no browser/UI tests, no live Stripe integration tests, and no load tests. Async-settlement methods (which pass through `processing`) are modeled but not exercised by an automated test.

### Single-method testing
Payment paths were verified with cards, because card test numbers deterministically drive the decline, SCA/3DS, and retry branches. Other methods surfaced by `automatic_payment_methods` (Link, Amazon Pay) resolve to the same PaymentIntent lifecycle and the same `succeeded` webhook, so they share the fulfillment path, but they were not each individually tested.
