# Voice order isolation

Different voice orders run concurrently, subject to the existing global limits
for client creation and native joins. Each account is leased exclusively to
one voice order while joining, connected, and leaving: an account cannot be
counted in two simultaneous voice chats. The scheduler waits when the only
eligible accounts are leased; sufficient distinct eligible accounts and host
resources are required to fill all orders concurrently.

A failed client teardown quarantines the account until process restart rather
than opening another connection with the same session. Investigate the logged
teardown error before restarting. FloodWait deadlines remain shared per account
and are not reset by ending an order.

These leases and task registries are **process-local**. Run only one voice
executor for a shared account pool; multiple bot replicas/hosts require a
shared, crash-safe distributed lease implementation before they are safe.
This does not guarantee Telegram/WebRTC connectivity; test with real calls in
the deployment network before production rollout.

## Diagnosing transport timeouts

`play()` completing is required for a new slot to count: a participant entry
by itself can be a stale ghost. `[VoiceJoinDiag]` logs distinguish a completed
transport exception from a still-pending join. A timeout is retried with pacing;
it is not proof of a bad account. Frozen/restricted accounts need attention in
Telegram and cannot be repaired by retries.

On the deployed server, compare `git rev-parse HEAD` with the deployed image,
rebuild the image after changing dependencies, and inspect the unfiltered bot
logs. Run `docker compose exec bot python tools/check_udp.py` and the same
command on the host to diagnose basic UDP reachability. DNS-over-UDP success
is not proof that Telegram's media endpoints are reachable. Inspect firewall,
WARP, NAT and provider routing if the TCP Telegram client connects but the
WebRTC transport times out. Do not change network mode or expose services
without reviewing the deployment's security settings.

Repeated stop requests are serialized per account and checked against the
order's active state or lease, so a stale stop cannot tear down a later order's
joining client. If native call enumeration/leave or client teardown cannot be
confirmed, the account remains quarantined and its lease remains held for a
cleanup retry. A retained completed join future is consumed before issuing any
new native join, including when the first wait timed out.

The server logs provided for incident 1203 report transport TimeoutError and a
separately frozen account; they do not identify why the transport timed out.
The later 1205 logs show joins succeeding, but report deployed commit 2d67ac8
and staged local changes, not this branch. Preserve those changes and reconcile
versions before deployment; do not overwrite the production checkout blindly.
