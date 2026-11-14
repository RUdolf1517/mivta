# Contributing

Thanks for considering a contribution. `mivta` has one rule that outranks
everything else:

> **`mivta` must never store or log user message content, must never touch a
> chat message the user did not explicitly mark for translation, and must never
> add signatures, watermarks, or conversational fluff to translated output.**

That rule decides most review outcomes before any style question comes up.

---

## 1. Before you write code

For anything larger than a typo or documentation fix:

1. Open an issue describing the bug, provider quirk, or proposed capability.
2. Check Section 2 below to make sure the idea does not cross a deliberate
   product boundary.
3. Wait for agreement on the behaviour and configuration contract before writing
   implementation code. Code written before the contract is agreed on is usually
   rewritten or discarded.

---

## 2. What this project will not accept

- Storing message text, inline queries, or translated strings in SQLite, files,
  or logs (only numeric `user_id`, language/style codes, and Secretary Mode
  connection metadata may be persisted).
- Translating direct messages, forwarded messages, or voice/audio notes sent to
  the bot in private chat — Inline Mode (`@BotUsername ...`) and Secretary Mode
  (`> ...`) are the only translation flows.
- Editing incoming interlocutor messages in Secretary Mode, or editing outgoing
  messages that do not start with an explicit `>` / `>lang` / `src>tgt` trigger.
- Appending attribution footers, links, ads, or LLM chatter around the
  translated text.
- Coupling `mivta/translator/` to `aiogram`, `aiosqlite`, or `mivta.config`
  (`tests/test_handlers_e2e.py` enforces this boundary via AST inspection).
- Adding runtime dependencies that lack prebuilt wheels on Linux `x86_64` /
  `aarch64` across Python 3.11–3.15 or require a Rust / C toolchain during
  `install.sh` on a small VPS.

Each of these is a product decision, not a style preference.

---

## 3. Development setup

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"

# Lint Python and installer script
.venv/bin/ruff check .
shellcheck install.sh

# Run the offline test suite
.venv/bin/pytest -q
```

All tests run offline using `httpx.MockTransport`, in-memory / `tmp_path`
SQLite databases, and a recording Telegram session — no `BOT_TOKEN` or external
API keys are needed to run the suite.

---

## 4. What a pull request must contain

| Change | Required in the same pull request |
| --- | --- |
| Bug fix | A regression test in `tests/` that fails before the fix |
| New translation provider | Implementation under `mivta/translator/`, `FallbackTranslator` compatibility, `MockTransport` unit tests in `tests/test_translators.py`, `.env.example` + `README.md` entries |
| New env var or installer flag | Validation in `mivta/config.py`, matching CLI flag in `install.sh` (if applicable), tests in `tests/test_config.py` / `tests/test_install_script.py`, `.env.example` + `README.md` update |
| Entity / formatting change | UTF-16 surrogate-pair (emoji) offset tests in `tests/test_formatting.py` |
| Installer or `systemd` change | `shellcheck install.sh` clean + E2E coverage in `tests/test_install_script.py` |
| User-visible behaviour change | `CHANGELOG.md` entry |
| Version bump | `mivta/__init__.py` + `pyproject.toml` updated together, matching the `vX.Y` release tag |

### Versioning

`mivta` uses a two-component `X.Y` version, where `X` is a feature release and
`Y` is a patch:

- **patch (`7.1`, `7.2`, …)** — bug fixes, documentation, UI polish, refactors,
  new tests: anything that does not add a new user-facing scenario;
- **feature (`8.0`)** — a new command, mode, provider, installer behaviour, or
  any other capability a user would notice;
- **tags** are `vX.Y`, and `mivta.__version__` must match the tag exactly —
  `.github/workflows/release.yml` fails the release otherwise.

Small pull requests are batched into one release instead of being tagged
individually, so version numbers stay meaningful instead of racing ahead.

Additionally:

- `ruff check .` and `shellcheck install.sh` must pass with zero warnings;
- GitHub Actions CI must be green across Python 3.11–3.14.

---

## 5. Code style

- Python 3.11+, formatted and checked with `ruff` (line length 100, full type
  annotations).
- Prefer small dataclasses (`frozen=True, slots=True`) and plain functions over
  deep class hierarchies.
- Telegram `MessageEntity` offsets and lengths are measured in **UTF-16 code
  units**, not Python code points — always use the helpers in
  `mivta/utils/formatting.py` when computing or rebasing spans.
- Never log `request.text`, translated output, `BOT_TOKEN`, or `TRANSLATOR_API_KEY`
  at any log level. `httpx` and `httpcore` loggers stay clamped to `WARNING` so
  query URLs are never written to `stderr` / `journald`.

---

## 6. Testing expectations

- Every behavioural change needs automated tests covering both the happy path
  and the refusal / error path (`401`, `429`, `5xx`, timeouts, oversized input,
  malformed JSON).
- Tests must never make real network requests, must not depend on wall-clock
  timing where an injected clock works, and must write only inside `tmp_path`.

---

## 7. Reporting bugs

Open a GitHub issue for non-security bugs and include:

- the release tag or commit hash, Python version, OS, and active
  `TRANSLATOR_PROVIDER`;
- whether the issue occurred in Inline Mode or Secretary Mode;
- minimal steps to reproduce (with tokens, API keys, and private chat content
  removed).

For suspected security vulnerabilities, follow [SECURITY.md](SECURITY.md) and
**do not** open a public issue.

---

## 8. Commit messages

Use concise, imperative commit subjects (Conventional Commits welcome):

```text
feat(translator): add mistral preset to OpenAI-compatible factory
fix(formatting): preserve UTF-16 offsets after adjacent emoji spans
docs: clarify Secretary Mode permission toggle in Telegram Android
test(install): verify rollback when post-update import check fails
```

Keep the subject line under 72 characters and explain *why* in the body when the
motivation is not obvious from the diff.

---

## 9. Licence

By contributing, you agree that your contribution is licensed under the MIT
Licence of this repository (see [LICENSE](LICENSE)). There is no CLA. Do not
copy code from projects under incompatible licences.
