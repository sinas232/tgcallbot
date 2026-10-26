# Hotfix 2026-10-26 — accounts never enter the voice call

`voice-join-2026-10-26.diff` is a plain `git diff` from the **deployed**
production tree to this branch's fixed tree. It contains only code, config,
docs and tests — no copy of itself.

* Base commit (must match the server): `2d67ac85b57bfcfd5d56f022c3566a1e86697516`
  (`arena/b3b7d55e-tgcallbot`, v2.3.23 — the only tree emitting
  `[VoiceUpdates] raw-only pipeline`, `[VoiceQuarantine]` and the VoiceDiag
  lines from the incident log).
* Generated from: `git diff --binary 2d67ac8 HEAD -- . ':(exclude)hotfix'`.
* Verified: `git apply --check` on a clean export of that commit passes, and
  the applied tree is byte-identical to `HEAD` minus `hotfix/`.

Apply (only when NO order is running — a restart drops all voice sessions):

```bash
cd /opt/tgcallbot
git status --porcelain                 # investigate a dirty tree first
git apply --check /path/to/voice-join-2026-10-26.diff
git apply /path/to/voice-join-2026-10-26.diff        # or: git apply --3way ...
docker compose config --quiet
docker compose up -d --force-recreate bot
```

Full diagnosis, verification commands and WARP/zombie-process checks:
[`docs/voice-join-incident-1405-08-04.fa.md`](../docs/voice-join-incident-1405-08-04.fa.md).

Files changed by the patch: `services/voice_call_manager.py`,
`services/session_client.py`, `services/voice_updates.py`, `config.py`,
`.env.example`, `CHANGELOG.md`, `docs/voice-join-incident-1405-08-04.fa.md`,
`tests/test_join_late_media_confirm.py`, `tests/test_session_close_resume.py`.
