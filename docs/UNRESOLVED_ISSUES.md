# Unresolved Issues

Last updated: 2026-04-25

1. Rejoined attacker bot damage does not always trigger recovery.
- Symptom: malicious bot can rejoin and continue destructive actions before a stable recovery request lifecycle catches up.
- Status: mitigated by attacker rejoin anomaly event + recent-ban fallback + pending attacker sync. Continue to monitor.

2. Defense reaction is too slow.
- Symptom: delayed neutralization under burst channel/webhook attacks.
- Status: mitigated by bounded-concurrency neutralization and adaptive ban pacing. Continue to monitor 429/error rate.

3. Recovery is too slow.
- Symptom: full server restoration takes too long during heavy incident windows.
- Status: mitigated by parallel snapshot loading, parallel independent recovery phases, and higher safe default concurrency.

4. Logging can still block critical paths.
- Symptom: high-frequency logs increase event-loop pressure under attack bursts.
- Status: mitigated by background Redis log writer; hot-path logging no longer performs Redis I/O inline.
