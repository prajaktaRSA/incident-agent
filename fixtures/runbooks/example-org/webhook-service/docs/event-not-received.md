# "Customer didn't receive event X"

Work through these in order; most reports end at step 1 or 2.

## 1. Is the endpoint subscribed to that event type?
Check `event_filters` for the site. If the event type isn't there, webhook-service filters the
event out by design (log line "event filtered: no endpoint subscribed"). The merchant needs to
add the event type in their webhook settings.

## 2. Was it actually delivered?
Search the delivery logs by `event_id`. A 2xx attempt means we delivered it: give the solutions
engineer the time, `request_id` and status so the merchant can find it on their side.

## 3. Is it still retrying?
If attempts failed and fewer than 7 were made, it's in the failed table waiting for the next retry.

## 4. No logs at all?
Check the producer's publish log and consumer lag on the event's topic (see consumer-lag.md).
