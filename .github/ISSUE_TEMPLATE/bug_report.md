---
name: Bug report
about: Incorrect translation flow, broken UTF-16 entity offset, or runtime failure
labels: bug
---

## What happened

<!-- Which mode were you using (Inline Mode `@bot ...` or Secretary Mode `> ...`),
     what input shape triggered the problem, and what actually happened?
     Remove any personal chat content, BOT_TOKEN, and API keys before posting. -->

## What you expected

## Environment

<!-- `--check` validates configuration and prints mode/provider/fallback
     without exposing any tokens or keys. -->

```console
$ python -m mivta --version
$ python -m mivta --check
```

- Mode: Inline Mode / Secretary Mode (Chat Automation) / Settings UI
- Telegram client and OS (e.g. Telegram Desktop 5.x on Linux, iOS, Android):
- How `mivta` was deployed (`install.sh` systemd, Docker / GHCR, manual `.venv`, or public `@mivtabot`):

## Relevant log lines (optional)

<!-- Paste sanitized output from `sudo journalctl -u mivta -n 30 --no-pager` if self-hosted. -->

```text

```
