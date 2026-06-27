/* Payment Element integration.
 *
 * Flow:
 *   1. On load, POST /create-payment-intent with the item id and a stable
 *      order_ref (idempotency token) -> receive the PaymentIntent client_secret.
 *   2. Mount the Payment Element with that client_secret.
 *   3. On submit, stripe.confirmPayment(...) with an absolute return_url.
 *      Stripe handles 3DS/SCA natively and redirects to /success with
 *      payment_intent in the query string.
 *
 * order_ref is generated once per page load and reused across submit retries,
 * so a double-click or dropped-network retry confirms the SAME intent rather
 * than creating a duplicate. We never trust or send the price from here.
 */
(function () {
  const form = document.getElementById("payment-form");
  const submitBtn = document.getElementById("submit");
  const errorEl = document.getElementById("error-message");

  const publishableKey = form.dataset.pk;
  const item = form.dataset.item;
  const returnBase = form.dataset.domain;

  const orderRef =
    (window.crypto && crypto.randomUUID && crypto.randomUUID()) ||
    "ref_" + Date.now() + "_" + Math.random().toString(36).slice(2);

  const stripe = Stripe(publishableKey);
  let elements;

  function showError(message) {
    errorEl.textContent = message || "Something went wrong. Please try again.";
  }

  function clearError() {
    errorEl.textContent = "";
  }

  function setLoading(isLoading) {
    submitBtn.disabled = isLoading;
    submitBtn.textContent = isLoading ? "Processing…" : "Pay now";
  }

  async function init() {
    try {
      const res = await fetch("/create-payment-intent", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ item: item, order_ref: orderRef }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        showError(body.error || "Unable to start checkout. Please return to the book list and try again.");
        return;
      }
      const { clientSecret } = await res.json();
      elements = stripe.elements({ clientSecret });
      const paymentElement = elements.create("payment");
      paymentElement.mount("#payment-element");
      clearError();
      submitBtn.disabled = false;
    } catch (e) {
      // Mounting can fail if this page is reopened for an intent that is no
      // longer confirmable (e.g. already paid via the back button). Fail
      // gracefully and point the customer back to a clean start rather than
      // showing a raw error on a dead Element.
      showError("This checkout session has expired. Please return to the book list and start again.");
      submitBtn.disabled = true;
    }
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitBtn.disabled) return;
    setLoading(true);
    clearError();

    const { error } = await stripe.confirmPayment({
      elements,
      confirmParams: { return_url: returnBase + "/success" },
    });

    // Reached only on immediate validation/card errors; on success the browser
    // is redirected to return_url before this resolves. Card declines and 3DS
    // failures surface here with a customer-readable error.message.
    if (error) {
      showError(error.message);
      setLoading(false);
    }
  });

  submitBtn.disabled = true;
  init();
})();