# Changelog

Все заметные изменения проекта документируются в этом файле.

## [0.4.0] — 2026-11-10

### Добавлено
- **Умная автозамена недоступных API с карантином (`FallbackTranslator` circuit breaker)**: при таймауте, ошибке `5xx` (`30 с`), исчерпании квоты `HTTP 429` (`60 с`) или ошибке авторизации `401/403` (`300 с`) проблемный провайдер уходит во временный карантин (`TRANSLATOR_COOLDOWN`), чтобы последующие запросы в Inline и Secretary Mode мгновенно шли в живой резервный API за `0 мс`, а после окончания карантина основной API автоматически возвращался в строй.
- **Мульти-ключевые цепочки `TRANSLATOR_CHAIN` и пресет `mistral`**: поддержка произвольных цепочек провайдеров со своими ключами, URL и моделями (`GROQ_API_KEY`, `GEMINI_API_KEY`, `MISTRAL_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `DEEPL_API_KEY`) с автоматической очисткой всех ключей в логах.
- **Бесключевой провайдер `mymemory` и резерв «из коробки»**: добавлен `MyMemoryTranslator` (`api.mymemory.translated.net`, до 50 000 симв./сут бесплатно с `MYMEMORY_EMAIL`); при стандартном `TRANSLATOR_PROVIDER=google` автоматическая страховка `google -> mymemory` включена по умолчанию без ключей.

## [0.3.1] — 2026-11-10

### Исправлено
- **Устойчивость бесключевого `google` на датацентровых VPS при `HTTP 429`**: `GoogleTranslator` теперь отправляет нейтральный браузерный `User-Agent` и при блокировке `translate.googleapis.com` (`429`/`403`/`5xx`) автоматически переключается на `clients5.google.com/translate_a/t?client=dict-chrome-ex` и `translate.google.com` (`client=at`), запоминая рабочий узел для последующих запросов без задержек.

## [0.3.0] — 2026-11-10

### Добавлено
- **Авто-локализация интерфейса бота (EN / RU)**: автоматический выбор языка всех меню, карточек Inline Mode, уведомлений Secretary Mode и списка команд Telegram по `from_user.language_code` (`ru`/`be`/`kk` → русский, остальные локали → английский) + ручное переключение в `/settings` и командой `/ui <en|ru|auto>`.
- **Коалесинг нажатий и отмена промежуточных запросов в Inline Mode (`INLINE_DEBOUNCE_MS`)**: короткое окно объединения нажатий (`180 мс` на промахе кэша) и мгновенная отмена незавершённого запроса пользователя при приходе более свежей строки — кратно экономит квоту LLM/DeepL и не сжигает rate-limit при быстрой печати.
- **Двусторонний авто-разворот языковой пары (`⇄`, `/swap`, `DEFAULT_SWAP_LANG`)**: если задан парный язык (например, `en ⇄ ru` или `en ⇄ pl`) и входной текст уже написан на целевом языке, бот автоматически переводит его в обратную сторону и в Inline Mode, и в Secretary Mode (`> текст`) без ручного `>lang` (мгновенно для разных алфавитов и за один вызов для LLM).
- **Публичная безопасность, `/privacy` и `/reset`**:
  - защита от закрытия тега в LLM через случайный одноразовый разделитель `<source_text_<nonce>>`, потолок ответа апстрима `1 MiB`, проверка HTTPS для ключей API и фильтр редактирования секретов в логах (`SecretRedactingFilter`);
  - разделение прав в `install.sh` (`root:root` `0755` на код/`.venv`/`install.sh`, `root:mivta` `0640` на `.env`, `mivta:mivta` `0700` + `UMask=0077` на `data/`);
  - команды `/privacy` (прозрачная политика хранения данных) и `/reset` (мгновенное удаление своих настроек из SQLite), а также флаги `python -m mivta --version` / `--check` и `Dockerfile HEALTHCHECK`.
- **Опциональный режим Webhook (`WEBHOOK_URL` + `WEBHOOK_SECRET_TOKEN`)**: встроенный HTTP-сервер на `aiohttp` с обязательной проверкой заголовка `X-Telegram-Bot-Api-Secret-Token` (автогенерация криптографического токена, если не задан вручную) и health-пробой `GET /healthz`.

## [0.2.1] — 2026-11-10

### Добавлено
- **Ежедневное автообновление и перезапуск в `06:00` (`systemd` timer)**: `install.sh` автоматически создаёт и включает `mivta-update.timer` (`OnCalendar=*-*-* 06:00:00`) и `mivta-update.service` (`--self-update`), которые каждое утро проверяют обновления из `main` с защитным откатом (rollback) при сбое импорта и штатно перезапускают `mivta.service` (настраивается через `--auto-update-time` / `MIVTA_AUTO_UPDATE_TIME`, отключается через `--no-auto-update` / `MIVTA_AUTO_UPDATE=0`).

## [0.2.0] — 2026-11-10

### Добавлено
- **Поддержка подписей к медиа в Secretary Mode (`caption`)**: отправка фото, документов и видео с подписью `> текст` (а также `>lang` и `src>tgt`) переводит подпись на лету через `editMessageCaption` с соблюдением лимита Telegram (`1024` символа).
- **Несколько карточек сразу в Inline Mode**:
  - автоматическая выдача 3 карточек регистров (`нейтральный`, `разговорный`, `формальный`) за **один** батч-запрос к LLM (или параллельно в DeepL) с дедупликацией совпадающих вариантов (`INLINE_STYLE_VARIANTS=true`);
  - выбор до 3 дополнительных языков карточек на пользователя через `/langs` и кнопку «Доп. карточки Inline» в `/settings` (или глобально через `INLINE_EXTRA_LANGS`).
- **Сохранение форматирования и защита кода (`entities`)**:
  - автоматическая защита `` `inline code` ``, fenced-блоков ```` ```code``` ````, URL-ссылок, email и `@username` от искажения переводчиками в обоих режимах;
  - перенос `MessageEntity` (`bold`, `italic`, `underline`, `strikethrough`, `spoiler`, `blockquote`, `text_link`, `code`, `pre`, `custom_emoji` и др.) с точным пересчётом UTF-16 смещений (включая суррогатные пары эмодзи) и нативная XML-разметка тегов в DeepL (`tag_handling=xml`).
- **Цепочка резервных провайдеров (`FallbackTranslator`)**: переменная `TRANSLATOR_FALLBACK_PROVIDER` (например, `google` или `libretranslate,google`) для мгновенного прозрачного переключения при таймауте, `429` или сбое основного API.
- **Быстрые LLM-пресеты и мини-глоссарий**:
  - готовые пресеты `groq` (`llama-3.3-70b-versatile`), `gemini` (`gemini-2.5-flash`) и `openrouter` (`openai/gpt-4o-mini`) без ручного указания URL/модели;
  - переменная `TRANSLATOR_GLOSSARY` со списком слов, брендов и имён, не подлежащих переводу (системное правило для LLM и токен-маскирование для классических MT-провайдеров).
- **GitHub Actions CI**: автоматическая проверка `ruff`, `shellcheck install.sh` и матрицы `pytest` на Python 3.11–3.14.

## [0.1.0] — 2026-11-10

### Добавлено
- Базовая реализация **Telegram Inline Mode** (`@BotUsername текст`) и **Telegram Secretary / Business Mode** (`> текст`) на `aiogram 3` + `httpx` + `aiosqlite`.
- Провайдеры `google`, `openai`, `deepl`, `libretranslate`, in-memory LRU+TTL кэш, singleflight-дедупликация и Token Bucket rate limiter.
- Автоматический установщик `install.sh` для Ubuntu/Debian с `systemd`-юнитом и поддержкой Python 3.11–3.15 без сборки Rust.
