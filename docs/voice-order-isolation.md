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
