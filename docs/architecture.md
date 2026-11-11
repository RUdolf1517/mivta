# Архитектура

```text
mivta/
├── __main__.py          # Точка входа `python -m mivta` (--version, --check)
├── bot.py               # Инициализация Bot/Dispatcher, Long Polling / Webhook, фильтр логов
├── config.py            # Загрузка и валидация .env, TLS-проверки URL и параметров Webhook
├── database.py          # Асинхронный SQLite (WAL): настройки пользователей и миграции схемы
├── handlers/
│   ├── pipeline.py      # Кэш, лимитер, waiter-counted singleflight и батчи стилей/языков
│   ├── inline.py        # Inline Mode: коалесинг нажатий, отмена устаревших задач, мульти-карточки
│   ├── business.py      # Secretary Mode: замена текста и подписей на месте без via @bot
│   └── commands.py      # Команды и интерактивные inline-меню настроек (RU / EN)
├── translator/          # Изолированный от Telegram и SQLite пакет переводчиков (HTTPX)
│   ├── base.py          # Протокол Translator, модели запросов/ответов и иерархия ошибок
│   ├── _http.py         # Потоковое чтение ответа с лимитом 1 MiB и маппинг HTTP-кодов
│   ├── openai.py        # OpenAI/Groq/Gemini/OpenRouter: одноразовый <source_text_nonce>, JSON-батчи
│   ├── deepl.py         # DeepL v2: нативный formality + сохранение XML-тегов <sN>
│   ├── google.py        # Бесключевой Google Translate с авто-переключением узлов при 429
│   ├── libre.py         # LibreTranslate API
│   ├── fallback.py      # Цепочка отказоустойчивости FallbackTranslator
│   └── factory.py       # Сборка провайдеров, пресетов и общего пула httpx.AsyncClient
└── utils/
    ├── cache.py         # In-memory LRU + TTL кэш с 16-байтными BLAKE2b ключами
    ├── formatting.py    # Маскирование кода/URL/@mention/глоссария и пересчёт UTF-16 entities
    ├── i18n.py          # Автовыбор языка UI (RU / EN) по локали клиента Telegram
    ├── languages.py     # Реестр языков, алиасов и распознавание Unicode-письменностей
    ├── parser.py        # Санитизация текста, разбор директив (>lang, src>tgt, !style) и сдвиг смещений
    └── ratelimit.py     # Token Bucket ограничитель частоты с фиксированным потолком памяти
```

---

## Поток обработки запроса

1. **Санитизация и разбор директив (`utils/parser.py`)**:
   удаляются непечатаемые управляющие символы, разбираются префиксы `>lang`, `src>tgt`, `!style`, а для Secretary Mode пересчитываются исходные UTF-16 смещения `MessageEntity` после отрезания префикса.
2. **Защита разметки и непереводимых участков (`utils/formatting.py`)**:
   код, ссылки, email, `@username` и слова глоссария заменяются бесколлизионными токенами `MIVTAK...X`, а вложенные стили (`bold`, `italic`, `text_link` и др.) оборачиваются в теги `<sN>...</sN>`.
3. **Авто-разворот пары (`utils/languages.py` + `handlers/pipeline.py`)**:
   если задан `swap_lang`, для разных алфавитов направление разворачивается до сетевого вызова, а для одного алфавита — внутри провайдера или конвейера по определённому языку.
4. **Коалесинг и дедупликация (`handlers/inline.py` + `handlers/pipeline.py`)**:
   в Inline Mode быстрые нажатия одного пользователя объединяются окном `INLINE_DEBOUNCE_MS` (на прогретом кэше — `0 мс`), устаревшая задача отменяется до списания лимита, а одинаковые параллельные запросы склеиваются через `InflightEntry` со счётчиком ожидающих корутин.
5. **Восстановление сущностей (`utils/formatting.py`)**:
   плейсхолдеры и теги `<sN>` разворачиваются обратно с пересчётом длин в **UTF-16 code units** (с учётом суррогатных пар эмодзи), чтобы Telegram корректно отобразил форматирование.
