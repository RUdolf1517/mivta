<div align="center">

# mivta

**מבטא** — *mivta*: произношение, выражение, интонация

**Перевод сообщений прямо в чатах Telegram: Inline Mode (везде) + Business Mode (без `via @bot`).**

[![python](https://img.shields.io/badge/python-3.11%2B-0b3d91?style=flat-square)](https://www.python.org/)
[![aiogram](https://img.shields.io/badge/aiogram-3.x-2ca5e0?style=flat-square)](https://docs.aiogram.dev/)
[![license](https://img.shields.io/badge/license-MIT-0b3d91?style=flat-square)](LICENSE)

</div>

---

`mivta` — минималистичный Telegram-бот для мгновенного перевода собственных сообщений прямо внутри переписки через официальный **Telegram Bot API** (без сторонних MTProto-клиентов и юзерботов):

1. **Inline Mode (`@BotUsername текст`)** — основной универсальный режим, работает в любых личных чатах, группах и каналах.
2. **Telegram Business Mode (`> текст`)** — дополнительный режим на базе официального Bot API для личных чатов 1-на-1: заменяет ваше отправленное сообщение переводом на лету через `editMessageText` **вообще без системной плашки `via @bot`**.

Что бот принципиально **не делает**:
- не переводит личные и пересланные сообщения в диалоге с самим ботом;
- не принимает голосовые сообщения и аудио;
- не добавляет к переводу подписи, ссылки или водяные знаки;
- не записывает тексты сообщений или переводов на диск и в логи (в SQLite хранятся только настройки языка/стиля пользователя и метаданные подключения Business Mode).

---

## Быстрый старт

### 1. Настройка бота в `@BotFather`

Создайте бота в [`@BotFather`](https://t.me/BotFather) командой `/newbot`, получите `BOT_TOKEN` и выставьте следующие параметры в меню бота (*Bot Settings*):

| Параметр в `@BotFather` | Рекомендуемое значение | Почему именно так |
| :--- | :--- | :--- |
| **Inline Mode** (`/setinline`) | **Enabled (Включён)** | Обязательно. Основной режим вызова `@BotUsername текст` в поле ввода любого чата. |
| **Inline Feedback** (`/setinlinefeedback`) | **Disabled (`0%`, выключен)** | `mivta` не собирает статистику `chosen_inline_result` и не логирует выбранные переводы. Отключение избавляет бота от лишних апдейтов с текстом ваших сообщений и повышает приватность. |
| **Business Mode** (*Bot Settings → Business Mode*) | **Enabled (Включён)** | Позволяет подключить бота к аккаунту с Telegram Premium (*Telegram для бизнеса → Чат-боты*) и переводить сообщения в личных чатах **без метки `via @bot`**. |
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

Нажмите на карточку с переводом — сообщение `Hello, how are you?` сразу отправится от вашего имени. *(Как у всех inline-ботов в Telegram, сервер Telegram автоматически отображает системный заголовок `via @BotUsername`).*

Быстрые одноразовые модификаторы прямо в строке ввода:

```text
@BotUsername >de Привет, мир!              # перевести это сообщение на немецкий
@BotUsername ru>pl Добрый день             # явная языковая пара: русский → польский
@BotUsername auto>ja Привет                # автоопределение → японский
@BotUsername !formal Добрый день, коллеги  # формальный регистр
@BotUsername >fr !casual Ну чё, погнали?   # французский + разговорный стиль
```

### Режим 2: Secretary Mode / Chat Automation (личные чаты 1-на-1, без `via @bot`)

Начиная с обновлений Telegram 2026 года, подключить бота к личному аккаунту можно даже **без подписки Telegram Premium** — в `@BotFather` эта настройка теперь называется **Secretary Mode** (ранее *Business Mode*), а в настройках приложения Telegram — **Автоматизация чатов / Chat Automation** (или *Telegram для бизнеса → Чат-боты*):

1. В `@BotFather` отправьте `/mybots` → выберите бота → **Bot Settings → Secretary Mode → Turn on** *(если открываете Mini App `@BotFather`: **Mode Settings → Secretary Mode**)*.
2. Если вы уже пробовали добавить бота до включения этой опции, **полностью перезапустите клиент Telegram** (иначе клиент может держать старый кэш прав бота).
3. В приложении Telegram откройте **Настройки → Автоматизация чатов** (*Settings → Chat Automation* / *Settings → Profile → Chat automation*, либо **Настройки → Telegram для бизнеса → Чат-боты**), введите `@BotUsername` и нажмите **Добавить**.
4. Выберите чаты (например, все личные чаты) и убедитесь, что в разрешениях включены **Чтение сообщений** и **Ответы на сообщения** (*Manage Messages → Reply to messages*).
5. В любом подключённом личном чате начните сообщение с префикса `>`:

```text
> Привет, как дела?                # перевод по вашим настройкам по умолчанию (/settings)
>de Привет, мир!                   # перевод на немецкий
ru>pl Добрый день                  # явная пара: русский → польский
> !formal Добрый день, коллеги     # стиль для конкретного сообщения
>fr !casual Ну чё, погнали?        # язык + стиль
```

Правила безопасности Business Mode:
- обычные сообщения без ведущего маркера `>` (включая `a > b`, `!Hola!` и др.) **не затрагиваются**;
- входящие сообщения собеседников **игнорируются** — обрабатываются только ваши собственные исходящие сообщения;
- если провайдер перевода недоступен или указан неверный код языка (`>zz`), бот **никогда не затирает сообщение в чате текстом ошибки**, а отправляет краткое уведомление только вам в личный чат с ботом.

### Персональные настройки пользователя

Настройки сохраняются в SQLite за `user_id` и действуют одинаково и в Inline Mode, и в Business Mode:

- `/settings` — интерактивное меню выбора языка перевода, исходного языка и стиля (также открывается кнопкой над inline-результатами);
- `/lang <code>` — язык перевода по умолчанию (`/lang en`, `/lang de`, `/lang pl`, `/lang zh`);
- `/source <auto|code>` — включить автоопределение (`/source auto`) или зафиксировать исходный язык вручную (`/source ru`);
- `/style <neutral|casual|formal>` — задать стиль перевода (`нейтральный`, `разговорный`, `формальный`).

---

## Провайдеры перевода

Компонент `mivta.translator` полностью независим от Telegram (`aiogram`) и SQLite. Все сетевые запросы выполняются асинхронно через `httpx.AsyncClient` с явными таймаутами и пулом соединений.

| `TRANSLATOR_PROVIDER` | Ключ API | Поддержка `style` (`neutral` / `casual` / `formal`) | Особенности |
| :--- | :---: | :---: | :--- |
| `openai` | Да | **Полная** (системная инструкция регистра, сленг, идиомы) | Совместим с OpenAI, OpenRouter, Groq, DeepSeek, локальным Ollama (`/v1/chat/completions`) |
| `deepl` | Да | **Нативная** (`default` / `prefer_less` / `prefer_more`) | Автоматически выбирает `api-free.deepl.com` для ключей с суффиксом `:fx` и `api.deepl.com` для Pro |
| `libretranslate` | Опционально | Нет (игнорируется провайдером) | Подходит для собственного self-hosted инстанса LibreTranslate |
| `google` | Не нужен | Нет (игнорируется провайдером) | Быстрый запуск «из коробки» только с `BOT_TOKEN` |

Примеры конфигурации провайдеров в `.env`:

```dotenv
# Вариант 1: OpenAI / совместимый API (Groq, OpenRouter, Ollama и т.д.)
TRANSLATOR_PROVIDER=openai
TRANSLATOR_API_KEY=sk-...
TRANSLATOR_API_URL=https://api.openai.com/v1
TRANSLATOR_MODEL=gpt-4o-mini

# Вариант 2: DeepL API (Free или Pro)
TRANSLATOR_PROVIDER=deepl
TRANSLATOR_API_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx:fx

# Вариант 3: Self-hosted LibreTranslate
TRANSLATOR_PROVIDER=libretranslate
TRANSLATOR_API_URL=http://127.0.0.1:5000
```

---

## Переменные окружения

| Переменная | По умолчанию | Описание |
| :--- | :--- | :--- |
| `BOT_TOKEN` | *(обязательна)* | Токен Telegram-бота от `@BotFather` |
| `TRANSLATOR_PROVIDER` | `google` | `google`, `openai`, `deepl` или `libretranslate` |
| `TRANSLATOR_API_KEY` | `""` | Ключ API (обязателен для `openai` и `deepl`) |
| `TRANSLATOR_API_URL` | *(по провайдеру)* | Кастомный базовый URL API переводчика |
| `TRANSLATOR_MODEL` | `gpt-4o-mini` | Имя модели для `openai`-совместимого провайдера |
| `TRANSLATOR_TIMEOUT` | `5.0` | Таймаут сетевого запроса к переводчику в секундах |
| `DEFAULT_SOURCE_LANG` | `auto` | Исходный язык по умолчанию для новых пользователей |
| `DEFAULT_TARGET_LANG` | `en` | Язык назначения по умолчанию для новых пользователей |
| `DEFAULT_STYLE` | `neutral` | Стиль по умолчанию (`neutral`, `casual`, `formal`) |
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
├── config.py          # Загрузка и строгая валидация переменных окружения и .env
├── database.py        # Асинхронное хранилище настроек и Business-подключений в SQLite (WAL)
├── handlers/
│   ├── pipeline.py    # Общий конвейер перевода (кэш, лимитер, singleflight, семафор)
│   ├── inline.py      # Обработчик Telegram Inline Mode
│   ├── business.py    # Обработчик Telegram Business Mode (замена сообщений без via @bot)
│   └── commands.py    # /settings, /lang, /source, /style и inline-клавиатуры настроек
├── translator/        # Независимый от Telegram слой провайдеров перевода (HTTPX)
│   ├── base.py        # Абстракция Translator, модели запросов/ответов и иерархия ошибок
│   ├── openai.py      # OpenAI-compatible провайдер с поддержкой стилей
│   ├── deepl.py       # DeepL v2 провайдер с поддержкой formality
│   ├── libre.py       # LibreTranslate провайдер
│   ├── google.py      # Google Translate JSON провайдер
│   └── factory.py     # Фабрика провайдеров и общего httpx.AsyncClient
└── utils/
    ├── cache.py       # Ограниченный по памяти TTL + LRU кэш с хешированными ключами
    ├── languages.py   # Реестр ISO-кодов языков, алиасов и нормализации
    ├── parser.py      # Санитизация ввода и разбор модификаторов Inline и Business режимов
    └── ratelimit.py   # Per-user Token Bucket ограничитель частоты с фиксированным потолком памяти
```

---

## Развёртывание

### Автоматическая установка на Ubuntu / Debian (`install.sh`)

Скрипт `install.sh` автоматически проверяет/доустанавливает системные зависимости (`python3 >= 3.11`, `python3-venv`, `git`, `ca-certificates`, включая поддержку Ubuntu 22.04 / 24.04 / 26.04+), создаёт непривилегированного системного пользователя `mivta`, разворачивает виртуальное окружение в `/opt/mivta/.venv`, защищает права `/opt/mivta/.env` (`0600`) и регистрирует `systemd`-сервис `mivta.service`.

**Интерактивная установка одной командой** (запросит `BOT_TOKEN` в терминале):

```bash
curl -fsSL https://raw.githubusercontent.com/RUdolf1517/mivta/main/install.sh | sudo bash
```

**Неинтерактивная установка** (для автоматизации и CI/CD):

```bash
curl -fsSL https://raw.githubusercontent.com/RUdolf1517/mivta/main/install.sh \
  | sudo BOT_TOKEN="123456:ABC..." TRANSLATOR_PROVIDER="google" MIVTA_NONINTERACTIVE=1 bash
```

Или с провайдером `openai` / `deepl`:

```bash
curl -fsSL https://raw.githubusercontent.com/RUdolf1517/mivta/main/install.sh \
  | sudo BOT_TOKEN="123456:ABC..." \
         TRANSLATOR_PROVIDER="deepl" \
         TRANSLATOR_API_KEY="xxxx:fx" \
         MIVTA_NONINTERACTIVE=1 bash
```

**Обновление установленного бота** (сохраняет `/opt/mivta/.env` и базу настроек `/opt/mivta/data/mivta.db`):

```bash
curl -fsSL https://raw.githubusercontent.com/RUdolf1517/mivta/main/install.sh | sudo bash
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
# Заполните BOT_TOKEN и параметры выбранного провайдера в .env
docker compose up -d --build
```

### Ручная настройка systemd (Linux VPS)

1. Установите проект в `/opt/mivta`:

```bash
sudo mkdir -p /opt/mivta
sudo useradd --system --home-dir /opt/mivta --shell /usr/sbin/nologin mivta
sudo git clone https://github.com/RUdolf1517/mivta.git /opt/mivta
sudo python3 -m venv /opt/mivta/.venv
sudo /opt/mivta/.venv/bin/pip install -U pip
sudo /opt/mivta/.venv/bin/pip install -r /opt/mivta/requirements.txt
sudo cp /opt/mivta/.env.example /opt/mivta/.env
sudo chmod 600 /opt/mivta/.env
sudo chown -R mivta:mivta /opt/mivta
```

2. Отредактируйте `/opt/mivta/.env` и создайте юнит `/etc/systemd/system/mivta.service`:

```ini
[Unit]
Description=mivta Telegram Inline & Business Translator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=mivta
Group=mivta
WorkingDirectory=/opt/mivta
EnvironmentFile=/opt/mivta/.env
ExecStart=/opt/mivta/.venv/bin/python -m mivta
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/opt/mivta/data

[Install]
WantedBy=multi-user.target
```

3. Запустите сервис:

```bash
sudo mkdir -p /opt/mivta/data && sudo chown mivta:mivta /opt/mivta/data
sudo systemctl daemon-reload
sudo systemctl enable --now mivta
sudo journalctl -u mivta -f
```

---

## Разработка и тесты

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -v
.venv/bin/ruff check .
```

## Лицензия

[MIT](LICENSE)
