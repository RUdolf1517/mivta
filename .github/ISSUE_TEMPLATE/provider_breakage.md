---
name: A translation provider stopped working
about: Upstream HTTP 429/403 block, changed JSON payload shape, or broken LLM preset
labels: bug
---

## Provider and preset

- `TRANSLATOR_PROVIDER`:
- `TRANSLATOR_FALLBACK_PROVIDER` (if set):
- Custom `TRANSLATOR_MODEL` or `TRANSLATOR_API_URL` (if overridden):

## What `mivta` logs now

<!-- Copy the warning/error line from `journalctl -u mivta` or container stderr.
     Never include API keys or tokens. -->

```text

```

## What worked before

<!-- Last `mivta` version or date when this provider worked from the same host. -->

## Network environment

- Hosting / network type (home connection, cloud VPS provider, corporate NAT):
- IPv4 only or dual-stack IPv4 + IPv6:
