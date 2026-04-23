# Unresolved Issues

Last updated: 2026-04-23

1. Rejoined attacker bot damage does not always trigger recovery.
- Symptom: malicious bot can rejoin and continue destructive actions before a stable recovery request lifecycle catches up.
- Status: partially mitigated, still observed intermittently.

2. Defense reaction is too slow.
- Symptom: delayed neutralization under burst channel/webhook attacks.
- Status: pending optimization of detection-to-action latency.

3. Recovery is too slow.
- Symptom: full server restoration takes too long during heavy incident windows.
- Status: pending concurrency and workflow optimization.

4. Logging can still block critical paths.
- Symptom: high-frequency logs increase event-loop pressure under attack bursts.
- Status: partially mitigated with deferred debug logs; full non-blocking log pipeline is pending.
