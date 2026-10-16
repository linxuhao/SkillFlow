"""Stale claim recovery.

Detection and recovery of stale claims has moved into the
SkillFlow.recover_stale_claims() method on the main class, which
uses the existing database connection and is called automatically
by advance_run().

The standalone function that opened its own connection was removed.
Use skillflow_instance.recover_stale_claims(threshold_seconds, run_id=run_id)
for a selected run; omit run_id only for explicit global/startup recovery.
"""
