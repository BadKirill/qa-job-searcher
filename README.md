# QA Job Searcher

Персональное веб-приложение и Telegram-бот для поиска QA-вакансий.

Веб-интерфейс: карточки по 10 на странице, архив и applied, эталон CV с подтверждением
фактов, диалог с помощником, генерация английского CV и письма на языке вакансии,
независимая проверка и скачивание PDF/DOCX.

Инструкции: [запуск веб-сервиса](docs/running-web.md),
[архитектура](docs/web-app-plan.md), [изоляция и MCP](docs/agent-runtime.md),
[правила тестирования](docs/testing-policy.md).

Telegram-бот сохраняет прежний интерфейс. Он собирает объявления из Remotive,
Himalayas, We Work Remotely, Remote.com, Hirify и прямых карьерных страниц компаний на
Greenhouse, Lever и Ashby. Дополнительно бот читает публичные Telegram-каналы из
`config/telegram_channels.toml`, оценивает релевантность и не присылает одну вакансию
повторно.

Профиль по умолчанию на Middle+ QA, automation/SDET, AI testing и
компании из fintech, digital assets, AI, SaaS и Web3. Подходят remote-позиции,
открытые для Worldwide, Europe, EU/EEA, EMEA или Serbia; для Serbia также
разрешён hybrid. Вакансии, ограниченные отдельной страной без Serbia,
исключаются. Бот проверяет весь текст вакансии: более узкое условие вида
`Germany only` имеет приоритет над общим `Remote Europe` в заголовке.

## Возможности

- `/search` — поиск по запросу;
- `/alerts_on` и `/alerts_off` — уведомления о новых совпадениях каждые 3 часа;
- `/alerts_on` использует недавний `/search` как базовую выдачу и не повторяет её;
- строгая фильтрация по полному тексту: Worldwide/Europe/EMEA remote и
  Serbia remote/hybrid;
- фильтр seniority: Junior/Entry-level/Intern исключаются, Middle/Medior и выше
  допускаются;
- Himalayas дополнительно очищается от вакансий, доступных только в других регионах;
- скоринг по роли, seniority, домену, навыкам, географии и наличию зарплаты;
- SQLite-дедупликация между источниками и после перезапуска;
- частичный отказ: если один источник недоступен, остальные продолжают работать;
- приватный allowlist по Telegram user ID;
- готовый запуск локально или через Docker Compose.

Бот не отправляет отклики автоматически. Wellfound намеренно не скрейпится: площадка
остаётся в `/sources` как ручная ссылка. Landing.jobs, No Fluff Jobs, CryptoJobsList,
YC Jobs, HelloWorld.rs и Infostud также доступны там для ручной проверки, пока для них
не подключён стабильный разрешённый API/фид. Instahyre тоже находится в `/sources` как
ручная ссылка: его персональная выдача требует входа в аккаунт и не имеет анонимного
публичного фида для фонового Docker-процесса.

## Быстрый запуск

1. Создай бота через [@BotFather](https://t.me/BotFather) и получи token.
2. Подготовь окружение:

   ```bash
   cp .env.example .env
   python3.11 -m venv .venv
   source .venv/bin/activate
   pip install .
   ```

3. Впиши token в `.env`. Переменные из `.env` не загружаются Python автоматически,
   поэтому для локального запуска экспортируй их:

   ```bash
   set -a
   source .env
   set +a
   qa-job-bot
   ```

4. Укажи свой числовой Telegram user ID в `TELEGRAM_ALLOWED_USER_IDS`. Его можно
   узнать в настройках Telegram или у доверенного бота, показывающего ID. Пустой
   список запрещён: бот не запускается без разрешённого пользователя.
5. Отправь `/search`, проверь выдачу и включи `/alerts_on`.

В Docker достаточно заполнить `.env` и выполнить:

```bash
docker compose up -d --build
docker compose logs -f bot
```

## Настройка поиска

Все критерии находятся в `config/search_profile.toml`. Здесь можно добавлять названия
ролей и навыки, менять географию и `min_score`. После изменения перезапусти процесс.

Прямые карьерные страницы перечислены в `config/job_boards.toml`. Идентификатор обычно
виден в URL:

```toml
[[greenhouse]]
name = "Company"
token = "company"
tags = ["fintech", "europe"]

[[lever]]
name = "Company"
token = "company"
region = "global" # или "eu"

[[ashby]]
name = "Company"
token = "company"
tags = ["ai"]
```

Публичные Telegram-каналы перечислены в `config/telegram_channels.toml`. Бот читает две
последние страницы публичного превью каждого канала; добавлять его администратором в
каналы не требуется.

Ошибочная или закрытая доска не ломает поиск: ошибка будет видна в `/stats` и в конце
ручной выдачи.

## Разработка и проверка

```bash
pip install '.[dev]'
pytest
ruff check .
```

Данные хранятся в `data/jobs.sqlite3`; файл и `.env` исключены из Git.

## Ограничения MVP

- Telegram работает через long polling: отдельный HTTPS webhook не нужен.
- Это персональный агрегатор, а не публичная перепубликация вакансий. В каждой карточке
  сохраняются источник и ссылка на оригинальное объявление.
- География в вакансиях часто написана свободным текстом. По умолчанию
  `allow_unknown_remote_location = false`, поэтому позиции с неясным регионом
  отбрасываются. Если включить эту опцию, они появятся с меткой
  `remote, географию стоит проверить`.
