# Merchant rate limiting (429)

## How it works
A 429 from a merchant sends the delivery to `webhook-ratelimited-events`, consumed by
`webhook-ratelimit-cg`, which honours `Retry-After`. Deliveries are delayed, not lost.

## Signs
- 429s in the logs for one endpoint, usually with `Retry-After`.
- Time lag rising on `webhook-ratelimited-events`.

## Actions
- Ask the merchant to raise their limit for our IPs, or accept the delay.
- If the merchant agrees to a lower rate, set a per-destination limit:
  `ops webhook ratelimit set --destination <destinationId> --rps <n>`.
