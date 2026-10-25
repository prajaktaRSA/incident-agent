# Internal webhook-config API errors

webhook-service loads each site's config from `GET /internal/sites/{site_id}/webhooks`
(Redis cache, 5s TTL). If it can't, the event goes straight to the DLQ as `CONFIG_FETCH_ERROR`.

## 401 Unauthorized
Caller key mismatch. Usually after a key rotation: the new key is in Secrets Manager but the
caller list in events-service wasn't updated (or the other way round).
- Check the latest events-service deploy for key or caller changes.
- Callers refetch the key on 401, so if the secret is right, a restart of webhook-service tasks
  isn't needed; fix the events-service side and redeploy.
- Afterwards replay the DLQ for `CONFIG_FETCH_ERROR` rows:
  `ops dlq replay --failure-type CONFIG_FETCH_ERROR --since <first failure>`.

## 5xx or high latency
Check events-service health and MySQL. Deliveries for all sites are affected.
