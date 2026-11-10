<div align="center">

# mivta

**מבטא** — *mivta*: произношение, выражение, интонация

**Перевод сообщений прямо в строке ввода любого Telegram-чата через Inline Mode.**

[![python](https://img.shields.io/badge/python-3.11%2B-0b3d91?style=flat-square)](https://www.python.org/)
[![aiogram](https://img.shields.io/badge/aiogram-3.x-2ca5e0?style=flat-square)](https://docs.aiogram.dev/)
[![license](https://img.shields.io/badge/license-MIT-0b3d91?style=flat-square)](LICENSE)

</div>

---

`mivta` — минималистичный Telegram-бот, который работает непосредственно в поле ввода любого чата через **Telegram Inline Mode**:

```text
@BotUsername Привет, как дела?
```

Бот возвращает готовый перевод во всплывающем окне над полем ввода. Одно нажатие — и переведённый текст отправляется обычным сообщением **от имени самого пользователя**, без подписей, рекламы, пересылки сообщений в личку и сторонних Telegram-клиентов.

Что бот принципиально **не делает**:
- не переводит личные и пересланные сообщения в диалоге с ботом;
- не принимает голосовые сообщения и аудио;
- не модифицирует переведённый текст ссылками или водяными знаками;
- не записывает тексты сообщений или переводов на диск и в логи (в SQLite хранятся исключительно настройки языка и стиля пользователя).

---

## Быстрый старт

### 1. Подготовка бота в `@BotFather`

1. Создайте бота в [`@BotFather`](https://t.me/BotFather) командой `/newbot` и получите `BOT_TOKEN`.
2. **Обязательно включите Inline Mode**: отправьте `@BotFather` команду `/setinline`, выберите бота и задайте подсказку (например, `Введите текст для перевода…`).

### 2. Локальный запуск

```bash
git clone https://github.com/RUdolf1517/mivta.git
cd mivta

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# Укажите BOT_TOKEN (и при необходимости провайдера перевода) в .env

.venv/bin/python -m mivta
```

---

## Использование

### Основной сценарий (в любом чате)

Введите в поле сообщения любого личного чата, группы или канала:

```text
@BotUsername Привет, как дела?
```

Нажмите на карточку с переводом — сообщение `Hello, how are you?` сразу отправится от вашего имени.

### Быстрые модификаторы прямо в строке ввода

Язык и стиль конкретного сообщения можно переопределить на лету без изменения настроек по умолчанию:

```text
@BotUsername >de Привет, мир!              # перевести это сообщение на немецкий
@BotUsername ru>pl Добрый день             # явная языковая пара: русский → польский
@BotUsername auto>ja Привет                # автоопределение → японский
@BotUsername !formal Добрый день, коллеги  # формальный регистр
@BotUsername >fr !casual Ну чё, погнали?   # французский + разговорный стиль
```

Обычная пунктуация (`a > b`, `> цитата`, `!Hola!`) распознаётся корректно и не перехватывается парсером директив.

### Персональные настройки пользователя

Настройки сохраняются в SQLite за `user_id` между вызовами бота. Открыть панель настроек можно кнопкой **«Настройки»** над списком inline-результатов или командами:

- `/settings` — интерактивное меню выбора языка перевода, исходного языка и стиля;
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
├── database.py        # Асинхронное хранилище настроек пользователей в SQLite (WAL)
├── handlers/
│   ├── inline.py      # Основной обработчик Telegram Inline Mode + singleflight-дедупликация
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
    ├── parser.py      # Санитизация ввода и разбор inline-модификаторов (>lang, !style)
    └── ratelimit.py   # Per-user Token Bucket ограничитель частоты с фиксированным потолком памяти
```

### Надёжность и приватность

- **Никаких блокировок event loop**: сетевой ввод-вывод через `httpx.AsyncClient`, работа с SQLite через `aiosqlite` в режиме `WAL`.
- **Дедупликация конкурентных запросов (singleflight)**: идентичные одновременные запросы разделяют один поход к API переводчика.
- **Ограниченное потребление памяти**: и кэш переводов, и таблица лимитера пользователей имеют жёсткий верхний предел размера; фоновая задача регулярно удаляет истёкшие по `CACHE_TTL` записи даже в периоды простоя, а при остановке бота кэш очищается.
- **Отсутствие утечек текста в логи**: логирование HTTP-клиента принудительно поднято до `WARNING`, чтобы URL-параметры не попадали в `stderr`/`journald`.

---

## Развёртывание

### Docker Compose

```bash
cp .env.example .env
# Заполните BOT_TOKEN и параметры выбранного провайдера в .env
docker compose up -d --build
```

Контейнер работает от непривилегированного пользователя `mivta` (`UID 10001`), а база настроек монтируется в именованный том `mivta-data`.

### systemd (Linux VPS)

1. Установите проект в `/opt/mivta`:

```bash
sudo mkdir -p /opt/mivta
sudo useradd --system --home-dir /opt/mivta --shell /usr/sbin/nologin mivta
sudo git clone https://github.com/RUdolf1517/mivta.git /opt/mivta
sudo python3 -m venv /opt/mivta/.venv
sudo /opt/mivta/.venv/bin/pip install -r /opt/mivta/requirements.txt
sudo cp /opt/mivta/.env.example /opt/mivta/.env
sudo chmod 600 /opt/mivta/.env
sudo chown -R mivta:mivta /opt/mivta
```

2. Отредактируйте `/opt/mivta/.env` и создайте юнит `/etc/systemd/system/mivta.service`:

```ini
[Unit]
Description=mivta Telegram Inline Translator
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
