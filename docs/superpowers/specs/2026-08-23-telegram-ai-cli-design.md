# telegram-ai-cli — design

**Дата:** 2026-08-23
**Статус:** черновик, ждёт утверждения владельца

## 1. Что это

AI-first CLI и MCP-сервер для управления **пользовательскими** аккаунтами Telegram
через MTProto. Одна кодовая база отдаёт три поверхности:

- **CLI** — человек в терминале (`tg-ai …`);
- **MCP-сервер** (stdio) — Claude Code и любой другой MCP-клиент;
- **Claude Code plugin** — установка сервера и скилов одной командой.

Описание репозитория уже опубликовано и связывает нас обещаниями:

> AI-first CLI and MCP server for Telegram user accounts over MTProto. Multi-account
> fleet with tdata and session import, task-shaped tools for Claude Code and any MCP
> client, out-of-band approval for dangerous actions, chat allowlist and audit log for
> every outgoing message.

Восемь свойств из него — обязательные, а не пожелания: мульти-аккаунтность, импорт
tdata, импорт готовых сессий, task-shaped тулы, out-of-band подтверждение опасных
действий, allowlist чатов, аудит **каждого** исходящего, работа и как CLI, и как MCP.

### Чего это НЕ

- Не Bot API-обёртка. Здесь пользовательский аккаунт, не бот.
- Не средство массовых рассылок, скрейпинга участников или обхода ограничений
  Telegram. Лимиты за сессию и allowlist существуют в том числе чтобы это было
  неудобно делать.
- Не конвертер форматов сессий. Парсинг tdata берётся готовый из `opentele-ng`.

## 2. Ключевое решение по зависимостям

У владельца уже четыре сущности вокруг «tdata → session», и новый проект **не должен
стать пятой**. Решение:

| Пакет | Роль здесь |
|---|---|
| `opentele-ng` (PyPI, репо `stufently/opentele`) | **единственная зависимость** по tdata: парсинг папки Telegram Desktop, device fingerprints, `ToTelethon()`. Живой, 352 теста, обновлялся 21.08.2026 |
| `tdata-session-exporter` (PyPI, репо `session-auth-lib`) | **НЕ используем.** Это bundle-based авторизация под инфраструктуру владельца с обязательным стабильным прокси — публичному продукту не подходит |
| репо `tdata-session-exporter` (отдельный, RU) | мёртвый дубль на заброшенном upstream — архивировать (отдельной задачей) |
| `telegram-tdata-session-converter` (private) | офлайн-конвертер 9 форматов, 580 тестов. Не конкурент и не зависимость; опубликовать отдельно |

То есть telegram-ai-cli — **потребитель** `opentele-ng`, а не новая реализация того же
самого.

Код работы с аккаунтами **портируется** (копированием, не зависимостью) из
`telegram-save-private-photo-video` (`tgsave/accounts/`, ~2000 строк, MIT, тот же
автор): `SessionLock`, `SessionPaths`, `sanitize_label`, `harden_path`,
`write_private_text`, `parse_proxy_url`, `resolve_api`, `build_client`,
`interactive_login`, `AccountRegistry`. Пакет `tgsave` не опубликован и решает другую
задачу — зависеть от него нельзя, а переписывать проверенный код заново незачем.

## 3. Стек

Проверено 23.08.2026, не по памяти:

| Компонент | Версия | Почему |
|---|---|---|
| Python | **3.14** | свежая поддерживаемая ветка (EOL 2030-10-31) |
| Telethon | **1.44.0** | последняя стабильная |
| opentele-ng | **1.4.0** | tdata |
| mcp (SDK) | **2.0.0** | последняя. Используем **только публичный API** |
| typer | **0.27.1** | CLI |
| pydantic-settings | **2.15.0** | конфиг YAML + env |
| cryptography | **50.0.0** | секреты at rest |
| cryptg | **0.6.0** | обязателен: AES-IGE на чистом Python блокирует event loop |
| python-socks[asyncio] | **3.0.0** | прокси Telethon |
| pytest / pytest-asyncio | **9.1.1** / latest | тесты |

`requires-python = ">=3.12"` — это НИЖНЯЯ граница для установки, а не пин; CI гоняет
матрицу 3.12–3.14, образ собирается на 3.14.

> **Важно:** боевой userbot владельца пинит `mcp==1.27.2`, потому что лезет в приватные
> атрибуты SDK. Здесь так делать нельзя — см. §6.

## 4. Архитектура

Образец — `zabbix-ai-cli` (`internal/opspec` + `internal/mcp` + `internal/cli`),
перенесённый на Python. Принцип оттуда дословно: *«MCP-тул — тонкий адаптер над теми же
операциями, что выполняет CLI. Второй реализации нет ни у чего.»*

```
telegram_ai_cli/
  __init__.py
  opspec.py        — реестр операций: Operation(name, cli, mcp_tool, summary, risk,
                     scope, params, run|plan). InputSchema() рендерит JSON Schema из
                     тех же Param — она же идёт в MCP registerTool, в флаги CLI и в
                     команду `tg-ai schema`
  ops/             — сами операции, по файлу на домен
    accounts.py  chats.py  messages.py  contacts.py  admin.py
  cli.py           — Typer: разворачивает реестр в дерево команд
  mcp_server.py    — stdio MCP: read-тулы + plan-тулы, никакой второй логики
  accounts/        — портировано из tgsave: loader.py, login.py, registry.py
  safety.py        — профили, allowlist чатов, лимиты за сессию, отказы
  plans.py         — стор планов (SQLite), создание/чтение/применение
  audit.py         — JSON-lines аудит
  redact.py        — PII-редактор
  config.py        — pydantic-settings: YAML + TGAI_ env overlay
  envelope.py      — единый JSON-контракт ответа
  errors.py        — иерархия ошибок + стабильные коды
```

Точка входа одна: `[project.scripts] tg-ai = "telegram_ai_cli.cli:main"`.
MCP поднимается как `tg-ai mcp`.

### Контракт ответа

Успех:

```json
{"ok": true, "data": {...}, "warnings": [],
 "meta": {"returned": 40, "total": 812, "truncated": true,
          "truncated_reason": "limit", "account": "work", "redacted": true}}
```

Ошибка:

```json
{"ok": false,
 "error": {"code": "FLOOD_WAIT", "message": "Telegram asked to wait 42s",
           "retryable": true, "retry_after": 42,
           "suggestion": "Retry after the wait, or use a different account"}}
```

Один и тот же envelope у CLI (`--json`) и у MCP. Лимиты публикуются в JSON Schema
(`min`/`max` у Param), а не режут молча.

## 5. Поверхность тулов

Task-shaped, мало и крупно — принцип zabbix-ai-cli («14 тулов, not two hundred»).
**Чтение — 8 тулов, запись — 3 plan-тула. Всего 11.**

### Read (выполняются сразу)

| Тул | Отвечает на вопрос |
|---|---|
| `telegram_fleet` | какие аккаунты есть, кто авторизован, где протухло, кто сейчас залочен |
| `telegram_chats` | какие диалоги есть у аккаунта / поиск по названию → chat_id |
| `telegram_chat_read` | история чата: страница назад через `before_id`, поиск по тексту, метаданные медиа |
| `telegram_inbox` | **что ждёт ответа прямо сейчас** по всему флоту: непрочитанное, упоминания, реплаи. Это главный task-shaped тул |
| `telegram_search` | глобальный поиск сообщений по всем чатам аккаунта |
| `telegram_whois` | резолв @username / id / инвайта → кто это, бот ли, общие чаты |
| `telegram_chat_members` | участники и админы чата (для админских задач) |
| `telegram_media_fetch` | скачать медиа конкретного сообщения в локальный путь |

Чтение **никогда** не помечает сообщения прочитанными: `mark_read` не вызывается ни
явно, ни как побочка (`send_read_acknowledge` запрещён на уровне кода и покрыт тестом).

### Write — только через план

Отдельного тула на каждую операцию записи нет. Есть три:

| Тул | Что делает |
|---|---|
| `telegram_plan_create(operation, params)` | ВАЛИДИРУЕТ и СОХРАНЯЕТ намерение. Ничего не отправляет. Возвращает `plan_id` и человекочитаемое описание того, что произойдёт |
| `telegram_plan_status(plan_id)` | состояние плана: pending / applied / rejected / expired |
| `telegram_plan_list()` | что сейчас ждёт решения человека |

`operation` — enum, генерируемый из того же реестра: `send_message`, `reply_message`,
`edit_message`, `delete_message`, `forward_message`, `mark_read`, `join_chat`,
`leave_chat`, `create_group`, `invite_user`, `promote_admin`, `set_profile`.
Поверхность MCP не растёт с числом операций.

## 6. Безопасность

Это инструмент, отдающий ИИ доступ к живому личному аккаунту. Модель угроз —
центральная часть продукта, а не приложение к нему.

### 6.1 Подтверждение опасного — вне контекста модели

**План может создать кто угодно. Применить его может только человек в терминале:**

```
tg-ai plan show 7c1f      # полный текст того, что уйдёт, и куда
tg-ai plan apply 7c1f     # ЕДИНСТВЕННЫЙ путь к отправке
```

У MCP-сервера **нет** тула, применяющего план. Не «есть, но требует confirm» — нет
вообще. Формулировка владельца из zabbix-ai-cli: *подтверждение, которое может прислать
агент, — это подтверждение, которое может прислать prompt injection*.

Почему не MCP elicitation: она идёт по тому же каналу, что и сам вызов, и её увидит тот
же контекст. Как **дополнительный** слой поверх плана — можно позже; как замена —
нет.

Почему не путь боевого userbot'а (DM владельцу с `y`/`n`): тот permission-relay стоит на
приватном API — кастомные notification-методы `notifications/claude/channel/*`,
подкласс `ServerSession` с переопределением защищённых `_receive_notification_type` /
`_received_notification`, прямой вызов приватного `server._handle_message`, и dev-only
флаг CLI `--dangerously-load-development-channels`. Для публичного продукта это
неприемлемо: сломается на первом апдейте SDK и не заработает ни в одном чужом клиенте.
**Переносится идея, не код.**

### 6.2 Профили

`--profile` / `TGAI_PROFILE`, дефолт — **`readonly`**:

| Профиль | Что можно |
|---|---|
| `readonly` (default) | только read-тулы. `plan_create` отказывает |
| `plan` | read + создание планов; применение только из терминала |
| `full` | то же, что `plan`, плюс `tg-ai send` напрямую из терминала без плана |

Профиля, в котором MCP-клиент применяет план, не существует.

### 6.3 Allowlist чатов

Конфиг (файл/env, **никогда** из содержимого сообщений):

```yaml
safety:
  allow_chats: [-1001234567890, 406685811]   # пусто = запись запрещена везде
  deny_chats: []                              # перебивает allow
  allow_new_chats_from_cli: true              # join/create из терминала
```

Пустой `allow_chats` означает «никуда», а не «куда угодно» — fail-closed.
Каждая операция объявляет свой `scope`, и allowlist'ы разные по опасности: у отправки
сообщений один список, у административных действий (`invite_user`, `promote_admin`) —
второй, у `join_chat` — третий. Единый список — это молчаливое расширение прав, у нас
это уже ловилось.

### 6.4 Лимиты за сессию

`max_sends_per_session`, `max_joins_per_session`, `max_admin_ops_per_session`
(дефолты 30 / 5 / 10). Слот **резервируется под локом ДО сетевого вызова** и
возвращается только при доказанном отсутствии эффекта — по whitelist'у **классов**
исключений Telethon, не по тексту сообщения. Проверка и инкремент по разные стороны
`await` — это гонка, на которой боевой сервер уже пробивал потолок.

### 6.5 Аудит

JSON-lines, одна строка на событие, ротация по размеру:

```json
{"ts":"2026-08-23T09:14:02Z","event":"message_sent","account":"work",
 "chat_id":-100123,"message_id":8801,"plan_id":"7c1f","actor":"cli",
 "text_sha256":"…","text_len":142}
```

Логируется **каждое** исходящее: отправка, правка, удаление, вступление, приглашение,
промоут, а также создание/применение/отклонение планов и все отказы гейтов. Текст
сообщения в аудит попадает хешем и длиной, а не телом — лог не должен становиться
вторым архивом переписки. `--audit-full` включает тело осознанно.

### 6.6 PII-редактор

Включён по умолчанию для всего, что возвращается наружу: телефоны (9+ цифр), email,
номера карт. `--raw` отключает. У боевого сервера такого нет вообще — здесь пишем с
нуля и покрываем тестами.

### 6.7 Хранение сессий

Портируется из tgsave без изменений: `sessions/` — `0700`, файлы — `0600`, атомарная
запись через `O_CREAT|O_EXCL` + fsync + `os.replace` (без окна между `open()` и
`chmod()`), симлинки не следуются. Эксклюзивный `flock` на файл, реально держащий auth
key, — не на label. `api_hash` и `proxy_url` шифруются AES-256-GCM (формат `enc:v1:`),
ключ из env или файла. 2FA-пароль берётся только с терминала через `getpass`, никогда
из argv или env. Одна и та же строка про device fingerprint: он генерируется один раз
детерминированно от label и замораживается в `<label>.api.json` — плавающий fingerprint
Telegram видит как угон сессии.

### 6.8 Границы, названные вслух

README и SECURITY.md обязаны сказать прямо: это автоматизация пользовательского
аккаунта; Telegram может ограничить или заблокировать аккаунт за автоматизацию;
`USE_CURRENT_SESSION` при импорте tdata **разлогинивает** оригинальный Telegram Desktop;
ответственность на пользователе. Без этого абзаца продукт публиковать нельзя.

## 7. Никаких наших данных

В репозиторий не попадает ничего из инфраструктуры владельца: номера телефонов, имена
личных и служебных аккаунтов, chat_id рабочих чатов, домены внутренних сервисов, хосты
прокси, имена боевых сессий. `.env.example` содержит только плейсхолдеры.

Проверка оформлена тестом `test_no_private_data.py`, а список конкретных запрещённых
строк лежит **вне репозитория** — в приватном файле, путь к которому берётся из
`TGAI_PRIVATE_DENYLIST`. Если переменная не задана, тест проверяет только структурные
признаки (телефоны в формате E.164, отрицательные chat_id, IP-адреса) и явно сообщает,
что полный список не подключён. Перечислять сами значения в публичном репо нельзя —
это ровно та утечка, которую тест должен предотвращать.

## 8. Оформление репозитория

По конвенциям владельца (`zabbix-ai-cli` + `yandex-mcp`):

```
README.md CHANGELOG.md SECURITY.md CONTRIBUTING.md CODE_OF_CONDUCT.md TASKS.md
LICENSE (MIT, уже есть)
pyproject.toml  Dockerfile  Makefile  .dockerignore  .env.example
.github/workflows/{ci.yml,release.yml}
.github/ISSUE_TEMPLATE/{bug_report.md,feature_request.md,config.yml}
.github/pull_request_template.md
telegram_ai_cli/…   tests/…   docs/*.md
.claude-plugin/{plugin.json,marketplace.json}
plugin.mcp.json
.claude/skills/<skill>/SKILL.md
```

README на английском, порядок разделов — как в zabbix-ai-cli: питч и бейджи → абзац
«что это и кому» → быстрый пример → «why not another MTProto wrapper» (там место для
конкретных сюрпризов Telegram: flood wait, revoked session, peer resolution, `noforwards`)
→ таблица Command → Answers → **Safety** с таблицей Caller × Read/Plan/Apply и примером
вывода плана → Install → Configure → «Add the MCP server to your AI client» (Claude Code,
Claude Desktop, Codex, Cursor, Docker) → список тулов → JSON-контракт → Documentation →
FAQ → Compatibility → License.

Плагин — по шаблону yandex-mcp: `.claude-plugin/plugin.json` +
`.claude-plugin/marketplace.json` + `plugin.mcp.json` с `${CLAUDE_PLUGIN_ROOT}`, установка
`/plugin marketplace add stufently/telegram-ai-cli` → `/plugin install telegram-ai-cli@stufently`.
Плагин ставит сервер и скилы, но не креды.

Topics на GitHub (сейчас пусто): `mcp`, `mcp-server`, `model-context-protocol`,
`claude-code`, `cli`, `ai-agents`, `ai-tools`, `telegram`, `mtproto`, `telethon`,
`userbot`, `python`.

## 9. CI и релизы

- `ci.yml`: `ruff check` + `ruff format --check`, `pytest` на матрице Python 3.12/3.13/3.14,
  **smoke** — реально поднимает `tg-ai mcp` по stdio и спрашивает `tools/list` (импорта
  модуля недостаточно: сервер не завершается, и старый фоновый шаг зеленел при падении),
  сборка Docker-образа с проверкой non-root старта.
- `release.yml`: только по тегу `v*`, никогда на push в main. Сборка sdist+wheel,
  публикация в PyPI через trusted publishing (OIDC, без хранимого токена), GHCR-образ,
  черновик GitHub Release с checksums.
- Сторонние actions пинятся **по commit SHA** с комментарием — теги у апстрима mutable.
- Версионирование semver по git-тегам. CHANGELOG в стиле Keep a Changelog,
  повествовательно, с секцией `### Security`.

## 10. Тесты

Целевое покрытие с первого релиза — по образцу того, что уже есть у обоих источников
(404 теста в tgsave, 383 в core боевого канала). Обязательные корпуса:

1. `test_safety.py` — allowlist (пустой = запрет), разные списки по scope, профили,
   отказ `plan_create` в readonly.
2. `test_limits.py` — резервирование слота до сети, возврат только по whitelist классов,
   гонка параллельных вызовов.
3. `test_plans.py` — план создаётся, не отправляет; применяется только из CLI;
   MCP не имеет пути к apply; TTL и повторное применение.
4. `test_accounts.py` — портированные тесты labels/proxy/lock/registry.
5. `test_redact.py` — телефоны, email, `--raw`.
6. `test_audit.py` — строка на каждое исходящее, хеш вместо тела.
7. `test_no_mark_read.py` — ни один путь чтения не помечает прочитанным.
8. `test_envelope.py` — единый контракт у CLI и MCP.
9. `test_no_private_data.py` — в репо нет данных владельца.
10. `test_mcp_surface.py` — набор и схемы тулов, отсутствие apply-тула.

Прогон только в Docker (`make test`), локальных установок нет.

## 11. Порядок работ

1. Скелет репо: pyproject, ruff, Dockerfile, Makefile, CI, пустой пакет, README-заглушка.
2. `accounts/` — порт из tgsave + тесты.
3. `opspec` + `envelope` + `errors` + `config` — каркас реестра.
4. Read-операции и CLI поверх них.
5. `safety` + `plans` + `audit` + `redact`.
6. Write-операции как планы, `tg-ai plan apply`.
7. `mcp_server` — тонкий адаптер, smoke-тест.
8. Плагин + скилы.
9. README, SECURITY.md, CHANGELOG, topics.
10. Тег v0.1.0, публикация.

Пункты 2–7 идут субагентами; 1 и 9 — основным потоком. Перед коммитом каждой значимой
части — ревью Codex и agy, два независимых ревью и **один** заход на исправления,
дальше решает владелец.

## 12. Решения по умолчанию (владелец может переиграть)

Ни одно из трёх не блокирует работу, поэтому взяты дефолты, а не отложены в вопросы:

1. **Контакт для SECURITY.md — GitHub Security Advisories**, приватный репорт через
   вкладку Security. Личная почта в публичный репозиторий не пойдёт: адрес оттуда
   выгребают скрейперы, а отозвать его потом нельзя. Захочет владелец явный email —
   добавим строкой.
2. **Имя команды — `tg-ai`**, пакет на PyPI — `telegram-ai-cli`. Короткое имя не занято
   в наших PATH; длинное `telegram-ai` остаётся свободным как алиас.
3. **Публикация откладывается до отдельного решения владельца.** Собираем репозиторий,
   CI и тег-триггер, но `v0.1.0` не ставим и в PyPI/MCP registry не публикуем: имя в
   публичном реестре занимается навсегда, а первый релиз инструмента, управляющего
   чужими аккаунтами, должен выходить осознанно, а не как побочка сборки.
