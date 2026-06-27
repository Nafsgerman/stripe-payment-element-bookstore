# Recommendations

How I would harden this into a production integration. Each item maps to a limitation in [`LIMITATIONS.md`](LIMITATIONS.md); the ordering is roughly the order I would tackle it, highest-leverage first.

### Move persistence to Postgres
Replace the SQLite file with Postgres and a connection pool. Keep the same shape (`orders` keyed on `pi_id`, the `processed_events` replay guard) but gain concurrent webhook handling, proper transactions, and migrations. Wrap the claim-event-then-transition sequence in a single transaction so a crash between them can't leave a claimed-but-unhandled event.

### Make fulfillment a real downstream step
Today the webhook handler advances order state and stops. In production the `succeeded` transition should trigger fulfillment: send a receipt, decrement inventory, and hand the order to an order-management system. Keep these as effects fired *from* the state transition, so the state machine stays the single source of truth and fulfillment is replay-safe (idempotent on order id).

### Add a terminal `canceled` state and reconciliation
Introduce a distinct terminal state fed by `payment_intent.canceled` (and expiry), so a genuinely dead payment is closed rather than left at `failed`/`processing`. Add a reconciliation job that periodically sweeps orders stuck in non-terminal states, re-fetches the PaymentIntent from Stripe, and resolves them, closing the gap if a webhook is ever missed entirely.

### Promote the catalog to a pricing service
Replace the hardcoded dictionary with a catalog/pricing table or service, with the server remaining the price source of truth. This unlocks dynamic pricing, multiple currencies, and tax/shipping calculation without changing the payment path; the amount is still computed server-side and passed to the PaymentIntent in minor units.

### Customers and saved payment methods
Attach a Stripe `Customer` to each PaymentIntent and store the customer id with the order, enabling saved cards and faster repeat checkout. This is the natural next feature once accounts exist, and it sits cleanly on the existing order row.

### Multi-method, configured from the Dashboard
The integration already uses `automatic_payment_methods`, so enabling wallets and local methods (Link, Amazon Pay, iDEAL, etc.) is a Dashboard change, not a code change. The work is in testing each method's settlement timing: the async/redirect methods that pass through `processing` need the success page to poll or rely on the webhook rather than assume synchronous completion.

### Resilience on outbound calls
Add application-level retry with backoff and a circuit breaker around Stripe calls, plus a dead-letter path for events that fail handling repeatedly. Combined with the reconciliation job above, this makes the system tolerant of transient Stripe/network failures rather than dependent on the SDK defaults.

### Observability and alerting
The code already logs every state transition with a request id. In production, ship those as structured logs, alert on orders stuck in `processing` past a threshold, and dashboard the webhook-vs-redirect timing so the race is monitored rather than assumed benign. Reconcile webhook delivery against Stripe's event log to catch anything dropped.

### Deployment and config
Run under a production WSGI server (e.g. gunicorn) rather than the Flask development server, with the bind port supplied by the platform's `PORT` environment variable and the public origin (`DOMAIN`) provided separately. In production, TLS terminates at a load balancer or reverse proxy, so the application's bind port and its public origin are different concerns. Secrets come from the platform's secret manager, never from a committed file.

### Broaden the test surface
Add integration tests against Stripe test mode (real signatures, real PaymentIntent lifecycle), browser/UI tests for the checkout and confirmation flows, and an automated test for the async `processing → paid` path. Load-test webhook handling to confirm the Postgres-backed claim-and-transition holds under concurrent delivery.
