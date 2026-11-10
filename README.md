<div align="center">

# mivta

**מבטא** — *mivta*: произношение, выражение, интонация

**Перевод сообщений прямо в чатах Telegram: Inline Mode (везде) + Secretary Mode (без `via @bot`).**

[![CI](https://github.com/RUdolf1517/mivta/actions/workflows/ci.yml/badge.svg)](https://github.com/RUdolf1517/mivta/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.11%2B-0b3d91?style=flat-square)](https://www.python.org/)
[![aiogram](https://img.shields.io/badge/aiogram-3.x-2ca5e0?style=flat-square)](https://docs.aiogram.dev/)
[![license](https://img.shields.io/badge/license-MIT-0b3d91?style=flat-square)](LICENSE)

</div>

---

`mivta` — минималистичный Telegram-бот для мгновенного перевода собственных сообщений прямо внутри переписки через официальный **Telegram Bot API** (без сторонних MTProto-клиентов и юзерботов):

1. **Inline Mode (`@BotUsername текст`)** — основной универсальный режим, работает в любых личных чатах, группах и каналах (поддерживает выдачу сразу нескольких карточек стилей или избранных языков).
2. **Secretary Mode / Автоматизация чатов (`> текст`)** — режим на базе официального Bot API для личных чатов 1-на-1: заменяет отправленные текстовые сообщения и подписи к фото/файлам (`caption`) переводом на лету **вообще без системной плашки `via @bot`**, сохраняя форматирование (`bold`, `italic`, ссылки, код).

Что бот принципиально **не делает**:
- не переводит личные и пересланные сообщения в диалоге с самим ботом;
- не принимает голосовые сообщения и аудио;
- не добавляет к переводу подписи, ссылки или водяные знаки;
- не записывает тексты сообщений или переводов на диск и в логи (в SQLite хранятся только коды языков/стиля пользователя и метаданные подключения Secretary Mode).

---

## Быстрый старт

### 1. Настройка бота в `@BotFather`

Создайте бота в [`@BotFather`](https://t.me/BotFather) командой `/newbot`, получите `BOT_TOKEN` и выставьте следующие параметры в меню бота (надёжнее через текстовую команду `/mybots` → выберите бота → **Bot Settings**):

| Параметр в `@BotFather` | Рекомендуемое значение | Почему именно так |
| :--- | :--- | :--- |
| **Inline Mode** (`/setinline`) | **Enabled (Включён)** | Обязательно. Основной режим вызова `@BotUsername текст` в поле ввода любого чата. |
| **Inline Feedback** (`/setinlinefeedback`) | **Disabled (`0%`, выключен)** | `mivta` не собирает статистику `chosen_inline_result` и не логирует выбранные переводы. Отключение избавляет бота от лишних апдейтов с текстом ваших сообщений и повышает приватность. |
| **Secretary Mode** (*Bot Settings → Secretary Mode*) | **Enabled (Включён)** | Позволяет подключить бота в **Настройки → Автоматизация чатов** (*Chat Automation*) и переводить сообщения и подписи к файлам в личных чатах **без метки `via @bot`**. |
| **Threaded Mode** (*Bot Settings → Threaded Mode*) | **Disabled (Выключен)** | Включать **не нужно**. Бот не ведёт диалоговых сессий в ЛС — при включённом Threaded Mode переход по кнопке «Настройки» из Inline Mode открывал бы лишний список топиков вместо мгновенного экрана `/settings`. |

### 2. Локальный запуск

```bash
git clone https://github.com/RUdolf1517/mivta.git
cd mivta

python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# Укажите BOT_TOKEN (и при необходимости провайдера перевода) в .env

.venv/bin/python -m mivta
```

---

## Использование

### Режим 1: Inline Mode (любые чаты, группы и каналы)

Введите в поле сообщения любого чата:

```text
@BotUsername Привет, как дела?
```

- При использовании провайдера с поддержкой стилей (`groq`, `openai`, `gemini`, `openrouter`, `deepl`) бот за один запрос покажет **до 3 карточек регистров** (`нейтральный`, `разговорный`, `формальный`).
- Если настроить дополнительные языки (`/langs pl de` или в меню `/settings`), бот сразу отдаст **карточки на нескольких избранных языках** для отправки в одно касание.

Быстрые одноразовые модификаторы прямо в строке ввода:

```text
@BotUsername >de Привет, мир!              # перевести это сообщение на немецкий
@BotUsername ru>pl Добрый день             # явная языковая пара: русский → польский
@BotUsername auto>ja Привет                # автоопределение → японский
@BotUsername !formal Добрый день, коллеги  # формальный регистр
@BotUsername >fr !casual Ну чё, погнали?   # французский + разговорный стиль
```

### Режим 2: Secretary Mode / Chat Automation (личные чаты 1-на-1, без `via @bot`)

Начиная с обновлений Telegram 2026 года, подключить бота к личному аккаунту можно даже **без подписки Telegram Premium** — в `@BotFather` настройка называется **Secretary Mode** (ранее *Business Mode*), а в настройках Telegram — **Автоматизация чатов / Chat Automation**:

1. В `@BotFather` отправьте `/mybots` → выберите бота → **Bot Settings → Secretary Mode → Turn on**.
2. Откройте личный чат со своим ботом, нажмите `/start`, затем в настройках Telegram откройте **Настройки → Автоматизация чатов** (*Settings → Chat Automation* / *Telegram для бизнеса → Чат-боты*) и вставьте точный `@BotUsername` (без `https://t.me/` и пробелов).
3. Выберите чаты и убедитесь, что включены **Чтение сообщений** и **Ответы на сообщения** (*Manage Messages → Reply to messages*).
4. В любом подключённом личном чате начните текстовое сообщение **или подпись к фото/документу/видео** с префикса `>`:

```text
> Привет, как дела?                # перевод по вашим настройкам (/settings)
>de Привет, мир!                   # перевод на немецкий
ru>pl Добрый день                  # явная пара: русский → польский
> !formal Добрый день, коллеги     # стиль для конкретного сообщения
```

### Сохранение форматирования, кода и ссылок

- В обоих режимах участки `` `inline code` ``, блоки ```` ```code``` ````, URL-ссылки (`https://...`), email и `@username` защищены от искажения переводчиком.
- В Inline Mode участки `` `code` `` и ```` ```code``` ```` автоматически преобразуются в нативные моноширинные `MessageEntity` Telegram.
- В Secretary Mode сохраняется форматирование Telegram (`bold`, `italic`, `underline`, `strikethrough`, `spoiler`, `blockquote`, `text_link`, `code`, `pre`, `custom_emoji`) с точным пересчётом UTF-16 смещений.

### Персональные настройки пользователя

Настройки сохраняются в SQLite за `user_id` и действуют одинаково и в Inline Mode, и в Secretary Mode:

- `/settings` — интерактивное меню выбора языка перевода, дополнительных карточек Inline, исходного языка и стиля;
- `/lang <code>` — основной язык перевода по умолчанию (`/lang en`, `/lang de`, `/lang pl`);
- `/langs <codes|off>` — до 3 дополнительных языков карточек в Inline Mode (`/langs pl de`, отключить: `/langs off`);
- `/source <auto|code>` — включить автоопределение (`/source auto`) или зафиксировать исходный язык (`/source ru`);
- `/style <neutral|casual|formal>` — задать стиль перевода (`нейтральный`, `разговорный`, `формальный`).

---

## Провайдеры перевода, резервирование и глоссарий

Компонент `mivta.translator` полностью независим от Telegram (`aiogram`) и SQLite.

| `TRANSLATOR_PROVIDER` | Ключ API | Стили (`neutral` / `casual` / `formal`) | Модель / Endpoint по умолчанию |
| :--- | :---: | :---: | :--- |
| `groq` | Да | **Полная** (1 батч-запрос на 3 стиля) | `llama-3.3-70b-versatile` (`api.groq.com/openai/v1`, ~150–250 мс) |
| `gemini` | Да | **Полная** (1 батч-запрос на 3 стиля) | `gemini-2.5-flash` (`generativelanguage.googleapis.com`) |
| `openrouter` | Да | **Полная** (1 батч-запрос на 3 стиля) | `openai/gpt-4o-mini` (`openrouter.ai/api/v1`) |
| `openai` | Да | **Полная** (1 батч-запрос на 3 стиля) | `gpt-4o-mini` (`api.openai.com/v1` или любой совместимый URL) |
| `deepl` | Да | **Нативная** (`default` / `prefer_less` / `prefer_more`) | Автовыбор `api-free.deepl.com` (`:fx`) или `api.deepl.com` + XML-теги |
| `libretranslate` | Опционально | Нет | Self-hosted или публичный инстанс LibreTranslate |
| `google` | Не нужен | Нет | Быстрый запуск «из коробки» только с `BOT_TOKEN` |

Пример конфигурации с быстрым пресетом Groq, автоматическим откатом на `google` при ошибках/лимитах и мини-глоссарием непереводимых терминов:

```dotenv
TRANSLATOR_PROVIDER=groq
TRANSLATOR_API_KEY=gsk_...
TRANSLATOR_FALLBACK_PROVIDER=google
TRANSLATOR_GLOSSARY=mivta, Kubernetes, PostgreSQL
```

---

## Переменные окружения

| Переменная | По умолчанию | Описание |
| :--- | :--- | :--- |
| `BOT_TOKEN` | *(обязательна)* | Токен Telegram-бота от `@BotFather` |
| `TRANSLATOR_PROVIDER` | `google` | `google`, `groq`, `gemini`, `openrouter`, `openai`, `deepl`, `libretranslate` |
| `TRANSLATOR_API_KEY` | `""` | Ключ API (обязателен для `groq`, `gemini`, `openrouter`, `openai`, `deepl`) |
| `TRANSLATOR_API_URL` | *(по пресету)* | Кастомный базовый URL API переводчика |
| `TRANSLATOR_MODEL` | *(по пресету)* | Имя модели LLM (переопределяет дефолт пресета) |
| `TRANSLATOR_TIMEOUT` | `5.0` | Таймаут сетевого запроса к переводчику в секундах |
| `TRANSLATOR_FALLBACK_PROVIDER` | `""` | Резервный провайдер или цепочка через запятую (например, `google`) |
| `TRANSLATOR_GLOSSARY` | `""` | Список слов/имён через запятую, которые не нужно переводить |
| `DEFAULT_SOURCE_LANG` | `auto` | Исходный язык по умолчанию для новых пользователей |
| `DEFAULT_TARGET_LANG` | `en` | Язык назначения по умолчанию для новых пользователей |
| `DEFAULT_STYLE` | `neutral` | Стиль по умолчанию (`neutral`, `casual`, `formal`) |
| `INLINE_EXTRA_LANGS` | `""` | Доп. языки карточек Inline по умолчанию (например, `pl,de`) |
| `INLINE_STYLE_VARIANTS` | `true` | Показывать карточки всех 3 стилей в Inline Mode |
| `DATABASE_PATH` | `data/mivta.db` | Путь к файлу SQLite с настройками пользователей |
| `MAX_TEXT_LENGTH` | `500` | Максимальная длина входного текста (`1..4096`) |
| `CACHE_TTL` | `300` | Время жизни кэша переводов в памяти в секундах (`0` — выкл.) |
| `CACHE_MAX_SIZE` | `512` | Максимальное число записей в LRU-кэше |
| `RATE_LIMIT_PER_MINUTE` | `30` | Допустимая средняя частота запросов к API на пользователя |
| `RATE_LIMIT_BURST` | `15` | Допустимый пиковый всплеск запросов при наборе текста |
| `MAX_CONCURRENT_TRANSLATIONS` | `20` | Лимит одновременных исходящих запросов к API переводчика |
| `LOG_LEVEL` | `INFO` | Уровень логирования (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

## Архитектура

```text
mivta/
├── __main__.py        # Точка входа `python -m mivta`
├── bot.py             # Инициализация Bot/Dispatcher, жизненный цикл, очистка кэша
├── config.py          # Загрузка и валидация .env, LLM-пресетов, резерва и глоссария
├── database.py        # Асинхронное хранилище настроек и подключений в SQLite (WAL)
├── handlers/
│   ├── pipeline.py    # Общий конвейер перевода (кэш, лимитер, singleflight, батчи стилей/языков)
│   ├── inline.py      # Обработчик Telegram Inline Mode (мульти-карточки стилей и языков)
│   ├── business.py    # Обработчик Secretary Mode (замена текста и подписей без via @bot)
│   └── commands.py    # /settings, /lang, /langs, /source, /style и inline-клавиатуры
├── translator/        # Независимый от Telegram слой провайдеров перевода (HTTPX)
│   ├── base.py        # Абстракция Translator, модели запросов/ответов и иерархия ошибок
│   ├── openai.py      # OpenAI/Groq/Gemini/OpenRouter провайдер (стили, батч JSON, глоссарий)
│   ├── deepl.py       # DeepL v2 провайдер (formality + XML-разметка тегов)
│   ├── libre.py       # LibreTranslate провайдер
│   ├── google.py      # Google Translate JSON провайдер
│   ├── fallback.py    # Цепочка отказоустойчивости FallbackTranslator
│   └── factory.py     # Фабрика провайдеров, пресетов и общего httpx.AsyncClient
└── utils/
    ├── cache.py       # Ограниченный по памяти TTL + LRU кэш с хешированными ключами
    ├── formatting.py  # Защита кода/ссылок/@username, глоссария и пересчёт UTF-16 entities
    ├── languages.py   # Реестр ISO-кодов языков, алиасов и нормализации
    ├── parser.py      # Санитизация ввода, разбор директив и сдвиг entities
    └── ratelimit.py   # Per-user Token Bucket ограничитель частоты с фиксированным потолком памяти
```

---

## Развёртывание

### Автоматическая установка на Ubuntu / Debian (`install.sh`)

**Интерактивная установка одной командой**:

```bash
curl -fsSL https://raw.githubusercontent.com/RUdolf1517/mivta/main/install.sh | sudo bash
```

**Неинтерактивная установка** (например, Groq + резерв на Google):

```bash
curl -fsSL https://raw.githubusercontent.com/RUdolf1517/mivta/main/install.sh \
  | sudo BOT_TOKEN="123456:ABC..." \
         TRANSLATOR_PROVIDER="groq" \
         TRANSLATOR_API_KEY="gsk_..." \
         TRANSLATOR_FALLBACK_PROVIDER="google" \
         MIVTA_NONINTERACTIVE=1 bash
```

**Обновление установленного бота** (сохраняет `/opt/mivta/.env` и `/opt/mivta/data/mivta.db`, а также включает ежедневный `systemd`-таймер автообновления и перезапуска `mivta-update.timer` в **06:00** по времени сервера):

```bash
curl -fsSL https://raw.githubusercontent.com/RUdolf1517/mivta/main/install.sh | sudo bash
```

Полезные команды таймера автообновления (`06:00`):

```bash
# Проверить расписание следующего запуска (и часовой пояс сервера):
sudo systemctl list-timers mivta-update.timer
timedatectl

# Изменить время ежедневного обновления/перезапуска (или отключить таймер):
curl -fsSL https://raw.githubusercontent.com/RUdolf1517/mivta/main/install.sh \
  | sudo MIVTA_AUTO_UPDATE_TIME="06:00" bash
# Отключить автообновление: sudo /opt/mivta/install.sh --from-local /opt/mivta --no-auto-update

# Запустить проверку обновлений и перезапуск вручную прямо сейчас:
sudo systemctl start mivta-update.service
```

**Удаление сервиса**:

```bash
# Удалить только systemd-юнит (сохранив .env и data/):
sudo /opt/mivta/install.sh --uninstall

# Полное удаление вместе с /opt/mivta и системным пользователем:
sudo /opt/mivta/install.sh --uninstall --purge
```

### Docker Compose

```bash
cp .env.example .env
docker compose up -d --build
```

---

## Разработка и тесты

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -v
.venv/bin/ruff check .
shellcheck install.sh
```

## Лицензия

[MIT](LICENSE)
