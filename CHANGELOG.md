# Changelog

Все заметные изменения проекта документируются в этом файле.

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
