# Merchant endpoint returning 5xx or timing out

## Symptoms
- One site's endpoint returns 500/502/503/504 or times out (10s), other endpoints are fine.
- Failed table grows for that `destinationId`; after 7 attempts events move to the DLQ.

## Confirm it's the merchant
1. Check service-wide `DeliveryFailures`: if it's flat, the problem is specific to the endpoint.
2. Check there was no webhook-service deploy right before the first failure.
3. In the logs, look at `time_taken_ms`: ~10000 means timeouts, a few hundred ms means the
   merchant answered with an error.

## What to do
- Tell the solutions engineer which endpoint, which status codes and since when. Give 2–3
  `request_id`s so the merchant can find them in their logs.
- Nothing to fix on our side. Retries continue automatically (7 attempts with backoff).
- Once the merchant confirms recovery, replay the DLQ for that destination:
  `ops dlq replay --tenant <site> --destination <destinationId> --since <first failure>`.
