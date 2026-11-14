# Security Policy

This document explains how to report a security vulnerability in **mivta** and
summarises the security boundaries the project maintains. It is not a claim that
the software is free of vulnerabilities.

## Supported versions

Security reports are investigated against the **latest published release** and
the current **`main` branch**. Fixes are developed on `main` and shipped in a
new release; backports to older tags are not guaranteed.

| Version | Supported |
| --- | --- |
| Latest release (`v0.3.x`) and `main` | Yes |
| Older releases (`< v7.0`, including the `0.x` series) | No — upgrade to the latest release |

Include the release tag or `python -m mivta --version` output in your report so
the affected code can be identified unambiguously.

## Report a vulnerability

**Do not disclose an unpatched vulnerability in a public issue, pull request, or
discussion.**

Use either of these private channels:

1. **Preferred:** open the repository's
   [Security tab](https://github.com/RUdolf1517/mivta/security) and choose
   **Report a vulnerability** to submit a private report through GitHub.
2. **Fallback:** email
   [rudolfzinovev@protonmail.com](mailto:rudolfzinovev@protonmail.com).

One channel is enough — please do not send duplicate reports. Never include live
`BOT_TOKEN` values, upstream API keys, or private Telegram conversations in a
report; use redacted output and a minimal synthetic reproducer instead.

## Scope

`mivta` sits in the path of outgoing Telegram messages and holds credentials for
Telegram's Bot API and an upstream translation API. Security-relevant issues
include:

- **Privacy / data-retention violations:** user message text, inline queries, or
  translated text written to SQLite, temporary files, or standard logs beyond
  the bounded in-memory TTL cache;
- **Credential exposure:** leakage of `BOT_TOKEN`, `TRANSLATOR_API_KEY`, or
  `TRANSLATOR_FALLBACK_API_KEY` into logs, Telegram error cards, upstream
  requests to unintended hosts, or world-readable files created by `install.sh`;
- **Unauthorized message mutation in Secretary Mode:** any path where
  `mivta` edits or deletes an incoming message from a chat interlocutor, a bot
  message, a message belonging to another `business_connection_id`, or an
  outgoing message lacking an explicit translation trigger;
- **Prompt or markup injection:** crafted input that escapes `<source_text>` or
  entity placeholders in a way that leaks configuration, corrupts unrelated
  entities, or bypasses length / rate-limit / concurrency bounds;
- **Denial of service / resource exhaustion:** unbounded memory growth in the
  cache, rate limiter, singleflight table, or HTTP client triggered by crafted
  inline queries or upstream responses;
- **Installer / privilege boundaries (`install.sh` & `systemd`):** unsafe file
  ownership or permissions under `/opt/mivta`, command injection via installer
  arguments or `.env` parsing, or bypass of `systemd` service sandboxing and
  rollback checks in `--self-update`.

### Out of scope (report as normal issues instead)

- Inaccurate translations, awkward register choices, or upstream provider rate
  limits (`HTTP 429` / `456`);
- Changes in unofficial upstream endpoints (such as the keyless `google`
  fallback endpoint);
- Telegram client UI behaviour (such as inline debounce timing or `@BotFather`
  menu renames);
- Compromised host root accounts or leaked `.env` files caused by operator
  misconfiguration outside `install.sh`.

## Credentials, permissions, and storage model

- **Privilege separation in `/opt/mivta` (`install.sh` + `systemd`):**
  - code, `.venv`, and `install.sh` are owned by `root:root` (`0755` / `0644`),
    so the unprivileged `mivta` service account cannot modify files executed as
    `root` by `mivta-update.service`;
  - `/opt/mivta/.env` is owned by `root:mivta` (`0640` in `systemd` mode, `0600`
    in single-user `--no-systemd` mode) — readable by the service, never
    writable by it, and inaccessible to other local users;
  - `/opt/mivta/data` is owned by `mivta:mivta` (`0700`, `UMask=0077`) and is
    the only `ReadWritePaths=` mount exposed inside `ProtectSystem=strict`.
- **SQLite (`data/mivta.db`)** stores only `user_id`, language/style codes, and
  `business_connections` status flags (`connection_id`, `user_id`,
  `user_chat_id`, `can_reply`, `is_enabled`). Message bodies are never stored.
- **RAM cache (`TranslationCache`)** keys entries by 16-byte `BLAKE2b` digests
  and purges expired entries via both lazy checks and a background janitor task.

## What to include

Provide as much of the following as is available:

- release version or commit hash, Python version, OS, and deployment method
  (`install.sh` + `systemd`, Docker Compose, or manual virtualenv);
- active `TRANSLATOR_PROVIDER` (with keys redacted);
- exact input or HTTP mock payload that triggers the issue;
- what you expected, what happened, and the concrete security impact.

## Research and coordinated disclosure

Good-faith security research is welcome:

- test only against your own Telegram bot instance and accounts you own or are
  explicitly authorised to test;
- never target third-party bots, users, or upstream translation providers with
  traffic or exploitation beyond a minimal local proof of concept (every
  component in `mivta` can be reproduced offline with `pytest` and
  `httpx.MockTransport`).

Reports are reviewed and coordinated with the reporter through validation, fix
development, and release disclosure. Response and remediation timelines depend on
severity and complexity; this policy does not promise a fixed SLA, but we aim to
keep reporters informed and agree on disclosure timing before public discussion.
