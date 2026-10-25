Consumer lag on webhook-delivery-cg
===================================

Alarms: `EstimatedMaxTimeLag` (warn 30s / crit 120s) and `MaxOffsetLag` (warn 500 / crit 2000).

### Offset lag high but time lag low
This is a burst. Consumers are keeping up; the backlog drains on its own. Common at month-end
billing. Watch for 15 minutes; no action unless time lag starts rising.

### Time lag rising
Consumers are slow or stuck. In order:
1. Was webhook-service just deployed? → see rollback-a-bad-deploy.md
2. Are deliveries slow (`DeliveryLatencyMs` p99 up)? Slow merchant endpoints hold consumer threads.
3. Is the config API slow? Every event needs its config (Redis TTL is only 5s).
4. Check ECS task count and CPU. Scale out: `ops ecs scale webhook-service --count +4`.

### An event "never arrived" and has no delivery logs
If the producer logged the publish but webhook-service has no line for the event, it is still
sitting in Kafka behind the lag. It will be delivered when the consumer catches up.
