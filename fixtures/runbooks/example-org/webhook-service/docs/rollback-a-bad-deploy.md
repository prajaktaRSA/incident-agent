# Rolling back a bad webhook-service deploy

**When:** failures, latency or lag jump for *all* sites within ~30 minutes of a deploy.

**Steps**
1. Post in #webhook-incidents that you're rolling back, with the PR link.
2. Re-run the previous successful "Deploy to ECS" workflow run, or:
   `ops ecs rollback webhook-service --to-previous`
3. Watch `DeliveryFailures` and `EstimatedMaxTimeLag` for 10 minutes.
4. Failed deliveries retry automatically; replay the DLQ for the incident window afterwards.
5. Revert the PR on main so the next deploy doesn't bring it back.
