# Эксплуатационная подготовка личных кабинетов

Этап 9, 12 ноября 2026. Это инструкция для PostgreSQL runtime. Рабочее
переключение старой SQLite, запуск Telegram и приглашение реальных участников
остаются этапом 10. Проверки с управляемым провайдером не подтверждают live OpenAI.

## Сервисы и приватные данные

`compose.runtime.yaml` запускает PostgreSQL 17, одноразовые миграции, API с собранным
React, scheduler и отдельные search/chat/document workers. `compose.yaml` сохраняет
старый запуск; не запускайте его рядом с новым runtime. Старый `qa-job-bot` отказывает
при заданном `DATABASE_URL` без явного `TELEGRAM_ACCOUNT_MODE=personal`. Персональный
polling-путь подготовлен и проверен на управляемом Telegram API; его живое включение
после переноса привязок и остановки старого процесса остаётся этапом 10.

Образ `Dockerfile.runtime` собирает frontend на Node 22, использует Python 3.12 и
PostgreSQL client 17, устанавливает зависимости из `requirements.lock`. Приложение
работает с UID/GID 10001; контейнеры приложения не получают дополнительные capabilities.
PostgreSQL не публикует порт. API доступен только через loopback хоста. Для внешнего
доступа нужен отдельно настроенный HTTPS reverse proxy и правильный `WEB_ORIGINS`.
Публикация на новом хостинге этой инструкцией не выполняется.

Named volumes `database`, `personal-data`, `backups` содержат базу, личные файлы и
резервные копии. Копия содержит чувствительные данные, включая серверную конфигурацию
из data, если она там есть. Доступ к Docker и этим volumes предоставляют только
оператору; внешние копии нужно хранить в приватном зашифрованном хранилище.
Архивы и конфигурацию не добавляют в Git и публичные CI artifacts.

## Первый запуск

Создайте `.runtime/runtime.env` с правами 0600. Для пароля используйте случайную
hex-строку: он подставляется в PostgreSQL URL, поэтому произвольные спецсимволы
требуют URL-кодирования. В файле должны быть:

```dotenv
POSTGRES_PASSWORD=<случайная-hex-строка>
POSTGRES_DB=job_searcher
WEB_PORT=8000
WEB_ORIGINS=http://127.0.0.1:8000
```

Дальше во всех командах сохраняйте один project name и один env file:

```bash
docker compose -p job-searcher --env-file .runtime/runtime.env -f compose.runtime.yaml build
docker compose -p job-searcher --env-file .runtime/runtime.env -f compose.runtime.yaml up -d --wait postgres
docker compose -p job-searcher --env-file .runtime/runtime.env -f compose.runtime.yaml run --rm migrate
docker compose -p job-searcher --env-file .runtime/runtime.env -f compose.runtime.yaml run --rm maintenance qa-job-import bootstrap --login owner --display-name Owner
docker compose -p job-searcher --env-file .runtime/runtime.env -f compose.runtime.yaml up -d --wait
```

Bootstrap создаёт только первого администратора, не импортируя историю SQLite.
Для переноса используйте `legacy-import.md` и порядок этапа 10. После запуска
прочитайте `/app/data/setup-token` внутри API-контейнера в приватном терминале;
введите его и новый пароль в интерфейсе. Не записывайте token в общие логи.
Пользователи получают приглашения через кабинет администратора.

OpenAI использует один серверный ключ. Подключите `openai-api-key` и
`openai-agent.json` в `/app/data` с владельцем 10001 и правами 0600. Формат
конфигурации описан в `agent-runtime.md`; ключ не передаётся в браузер. Без этой
конфигурации поиск работает, а платные задачи завершаются контролируемой ошибкой.
Readiness показывает готовность приложения и схемы, не доступность провайдера.

В managed cloud при сборке используйте штатный proxy и временные CA mounts:

```bash
docker build --secret id=proxy_ca,src="$CODEX_PROXY_CERT" --secret id=system_ca,src=/etc/ssl/certs/ca-certificates.crt -f Dockerfile.runtime -t job-searcher-runtime:local .
```

CA не копируется в image layers. Для runtime-сетей используйте доверие и proxy,
предусмотренные конкретным клиентом и политикой среды; не отключайте TLS-проверку.

## Остановка и запуск

```bash
docker compose -p job-searcher --env-file .runtime/runtime.env -f compose.runtime.yaml stop api scheduler search-worker chat-worker document-worker
docker compose -p job-searcher --env-file .runtime/runtime.env -f compose.runtime.yaml up -d --wait
```

Остановка даёт исполнителям 120 секунд. При SIGKILL платная задача с неизвестным
исходом не отправляется заново автоматически. Истёкший search с закреплёнными
входами может восстановиться, максимум до трёх claims. Для backup после жёсткой
остановки дождитесь истечения активных leases; стандартная lease — 90 секунд.
`stop` сохраняет volumes; `down --volumes` удаляет их и допустим только для
явно одноразовых синтетических стендов.

## Диагностика

`GET /health/live` возвращает только `alive`. `GET /health/ready` проверяет
подключение и текущую миграцию; при сбое возвращает 503 без DSN и traceback.
Healthcheck API использует Host из первого `WEB_ORIGINS`, поэтому внешний порт
может отличаться от внутреннего 8000.

Workers и scheduler обновляют сигнал каждые 10 секунд. Healthcheck проверяет
приватный локальный marker конкретного процесса и соответствующий сигнал в БД
не старше минуты. Marker предыдущего процесса перезаписывается новым ID; чужой
живой процесс не делает остановленный контейнер здоровым. При нормальном выходе
marker и запись удаляются; после SIGKILL старая запись перестаёт учитываться
через минуту. Старые записи пока не удаляются автоматически.

Администратор открывает «Аккаунт и доступ → Расписание и лимиты → Открыть
диагностику». `GET /api/admin/diagnostics` возвращает:

- количество заданий по виду/статусу и возраст самого старого ожидающего;
- число активных заданий с истёкшей lease;
- состояния API reservations, число вызовов без точного usage, остаток cooldown;
- число ошибок источников за всю историю и количество свежих сигналов сервисов.

Ответ не содержит CV, фактов, запросов чата, полных ошибок провайдера, ключей,
session IDs, имён пользователей или task IDs. Обычный аккаунт получает 403.
Счётчики usage и ошибок источников накопительные; это не отчёт стоимости и
не точный мониторинг доступности внешних сайтов. Обновление панели ручное.
Отсутствие точного usage не превращается в нулевой расход.

## Резервная копия базы вместе с файлами

Это offline backup: остановите API, scheduler, все workers, Telegram и любые
внешние процессы записи. `--services-stopped` подтверждает выполненное действие,
а не останавливает процессы. Нельзя использовать эту команду для online backup.
Утилита дополнительно запрещает активные leases и проверяет неизменность файлов,
но не может обнаружить каждый сторонний писатель в БД.

```bash
docker compose -p job-searcher --env-file .runtime/runtime.env -f compose.runtime.yaml stop api scheduler search-worker chat-worker document-worker
docker compose -p job-searcher --env-file .runtime/runtime.env -f compose.runtime.yaml run --rm maintenance python -m qa_job_bot.runtime_backup backup /backups/before-pilot --services-stopped
docker compose -p job-searcher --env-file .runtime/runtime.env -f compose.runtime.yaml up -d --wait
```

Каталог назначения должен отсутствовать. Копия содержит `postgres.dump`, дерево
`files` и `manifest.json` с revision/schema, исходным абсолютным data path,
размерами и SHA-256. Каталоги создаются 0700, файлы — 0600. Симлинки и специальные
файлы запрещены. Ошибка dump или изменение файла удаляет неполную новую копию.
Все промежуточные каталоги `files` также создаются с правами 0700 при любой
исходной umask, включая 0000 и 0022. Права не полагаются на окружение оператора.
PG-пароль передаётся утилитам через окружение, не через аргументы процесса;
stderr PostgreSQL не печатается в общие логи. Копируйте весь каталог, не только dump.

## Восстановление

Остановите все сервисы и сохраните текущую БД и files отдельно. Создайте новую
пустую PostgreSQL database; утилита отказывает при наличии прикладных объектов.
Подготовьте пустой volume personal-data для восстановления, сохранив прежний volume.
Внутри контейнера data root должен сохранять прежний абсолютный `WEB_DATA_DIR`
(по умолчанию `/app/data`, для пилота `/app/data/current`): пути в записях
templates/artifacts сохраняются. Restore не переносит их на другой путь.

Создайте отдельный env file для recovery с новым `POSTGRES_DB`; создайте эту БД
через `createdb` в существующем PostgreSQL либо поднимите отдельный recovery project
с пустым database volume. Подключите копию в `/backups`. Никогда не очищайте рабочий
volume, чтобы пройти проверку пустоты.

```bash
docker compose -p job-searcher-recovery --env-file .runtime/recovery.env -f compose.runtime.yaml up -d --wait postgres
docker compose -p job-searcher-recovery --env-file .runtime/recovery.env -f compose.runtime.yaml run --rm maintenance python -m qa_job_bot.runtime_backup restore /backups/before-pilot --services-stopped
docker compose -p job-searcher-recovery --env-file .runtime/recovery.env -f compose.runtime.yaml up -d --wait
```

У recovery project свой backups volume: заранее скопируйте полный каталог копии
в него, сохранив приватные права. Не запускайте `migrate` до restore в новую БД.
Compose запустит миграционную проверку после восстановления перед API/workers.
Сначала проверяются manifest, schema, data path, все файлы и dump. `pg_restore`
идёт одной транзакцией с остановкой при первой ошибке; `--clean --if-exists`
нужны для восстановления схемы `public` в уже созданную пустую БД. Непустая
база отклоняется до вызова этой команды.

Файлы готовятся во временном каталоге внутри пустого volume, после восстановления
БД публикуются в data root. Это не единая распределённая транзакция: при сбое
диска/процесса между commit БД и публикацией files сервисы оставляют остановленными,
сохраняют частичный результат для диагностики и повторяют restore в новую пустую
БД и новый пустой volume. Исходная копия и прежние данные не удаляются.

Проверьте вход, личную выдачу, CV, PDF preview/download и отрицательный доступ
чужого аккаунта перед переключением трафика. Telegram после восстановления
самостоятельно не запускается.

## Проверки и live smoke

```bash
QA_TEST_DATABASE_URL=postgresql://job_searcher:local-test-only@127.0.0.1:55432/job_searcher_test python -m qa_job_bot.quality
QA_TEST_DATABASE_URL=postgresql://job_searcher:local-test-only@127.0.0.1:55432/job_searcher_test python -m pytest tests/test_runtime_load.py -q -s
DATABASE_URL=<PostgreSQL-URL-из-приватного-окружения> python -m qa_job_bot.live_smoke --credentials-dir /private/config/data
```

Нагрузочный сценарий использует настоящий HTTP API, PostgreSQL, две профессии,
20 отдельных cookie sessions и отдельный процесс исполнителя. После SIGKILL двух
поисков запускает новый процесс, проверяет 20 поисков, 20 чатов, 20 документов,
PDF-тексты и отрицательные запросы чужих task/run/artifact IDs. Источники и OpenAI
управляемые. Обычная lease после рестарта — 90 секунд; у прерываемого процесса
для ускорения опыта — 1 секунда. HTTP keepalive клиента — 1 секунда, короче
серверного idle timeout. Отчёт `/tmp/job-stage9-load.json` содержит только числа;
его путь можно задать `QA_LOAD_REPORT`.

Live smoke требует реальных приватных файлов конфигурации и платных вызовов.
Он создаёт отдельную временную `qa_smoke_*` schema, два синтетических аккаунта,
отдельные web sessions и отдельные generator/reviewer/visual sessions. Проверяет
два ready-документа, тексты PDF и запреты чужого скачивания. Затем реальный reviewer
проверяет специально подготовленный черновик с неподтверждённым опытом; положительный
вердикт делает smoke неуспешным. Отрицательное задание блокируется без artifacts.
MCP вызывается через настоящий stdio каждым запросом gateway. После завершения
удаляются только собственная временная schema и синтетический data directory.

Программа не копирует ключ и не читает рабочие CV; полный ответ модели не печатает.
Итоговый JSON содержит только числа и результаты. Проверки самой команды с fixture
gateway подтверждают orchestration и fail-closed assertions, не live-интеграцию.
Для реального запуска используйте тот же исходный код и зависимости на хосте,
где доступна конфигурация; не заменяйте ими работающий deployment.


## Подготовленный персональный Telegram

Дополнительный сервис `telegram` выключен по умолчанию: он имеет отдельный Compose
profile. До этапа 10 не включайте его на рабочем токене. Не запускайте один токен
с разными копиями базы одновременно: PostgreSQL advisory lock действует внутри
одной БД, а не между независимыми восстановленными deployments.

Оператор проверяет принадлежность личного Telegram ID участнику и выполняет
явную привязку к уже активированному логину:

```bash
docker compose -p job-searcher --env-file .runtime/runtime.env -f compose.runtime.yaml run --rm maintenance python -m qa_job_bot.personal_telegram bind --chat-id 123456789 --login member
```

Новая привязка создаётся с уведомлениями off. Повторная привязка того же аккаунта
сохраняет deliveries и состояние подписки; `--alerts` / `--no-alerts` меняют его
явно. Чат другого аккаунта нельзя переназначить этой командой. Групповой отрицательный
chat ID, неактивированный/отключённый аккаунт и неизвестный логин отклоняются.
`enabled` у binding управляет автоуведомлениями, а не доступом привязанного
участника к ручным командам; отключение аккаунта отзывает и этот доступ.

Токен находится в `/app/data/telegram-bot-token`, владелец 10001, права 0600.
Профиль Telegram включается только в этапе переключения:

```bash
docker compose -p job-searcher --env-file .runtime/runtime.env -f compose.runtime.yaml --profile telegram up -d --wait telegram
```

Перед backup/restore остановите также этот сервис, если он был включён.
`/whoami` раскрывает только ID отправителя. Остальные команды требуют private chat,
совпадения sender/chat ID и явной привязки к активному аккаунту. `/settings`,
`/sources`, `/stats` читают личный PostgresStore. `/search` ставит личный поиск в
общую очередь; его завершение проверяется по сохранённому pending task, и результат
приходит в этот чат даже при выключенных автоуведомлениях. `/alerts_on` принимает
текущую выдачу за baseline, затем получает новые вакансии; `/alerts_off` прекращает
автоматическую отправку. Отправка ограничена пятью карточками за один проход;
Applied/Archive и уже подтверждённые доставки исключаются из автоуведомлений.
CV и документы этот транспорт не отправляет.

Один polling-процесс держит session advisory lock на весь цикл; второй отказывает
до `deleteWebhook/getUpdates`. Соединение проверяется перед polling и отправками,
offset сохраняется в БД. После сбоя процесса lock освобождается PostgreSQL, а offset,
pending и deliveries переживают рестарт. Падение после подтверждения Telegram, но
до записи delivery/offset, может привести к повторному сообщению: exactly-once
доставка внешним API не гарантируется. Логи содержат тип/статус ошибки без токена
и полных сообщений. HTTPX/httpcore debug/info для этого процесса отключены.

## Воспроизведение эксплуатационной репетиции

После сборки образа выполните `python tests/runtime_rehearsal.py`. Скрипт создаёт
два уникальных одноразовых Compose projects и свободные loopback ports. Проверяет
UID 10001, шесть healthy services, offline backup схемы public и файла, restore в
пустую БД и пустой named volume второго проекта, вход восстановленного аккаунта,
доступ к файлу по восстановленному абсолютному пути и stop/start. Живые OpenAI,
внешние источники и Telegram не вызываются. В finally удаляются только созданные
им синтетические projects и volumes; существующий deployment не используется.
Итог — `/tmp/job-stage9-compose.json` без персональных данных. CI имеет отдельный
job сборки образа и этой репетиции; основной job выполняет полный quality gate,
включая нагрузку 20 сессий и реальный PostgreSQL dump/restore в integration tests.

Live smoke сохраняет приватный JSON-журнал в `SMOKE_REPORT_DIR` (по умолчанию
`.runtime/live-smoke`): результат, агрегированные причины отказа и usage reservations
с session/turn IDs, без ключа, CV и полных ответов. Файлы создаются 0600. Если журнал
не удалось сохранить, schema не удаляется автоматически, соединение закрывается;
оператор сохраняет наблюдения перед ручной очисткой только этой `qa_smoke_*` schema.
Неизвестный usage сохраняется как unknown. Эти приватные журналы не загружаются в
общие CI artifacts. В managed proxy-среде live-команду запускают с явным
`OPENAI_USE_ENV_PROXY=1` и доверенным системным CA. В Docker для такого режима нужно
отдельно подключить runtime CA и proxy; build CA mounts не являются runtime trust store.


Этап 10 добавляет [pilot-cutover.md](pilot-cutover.md): обслуживание, ограничение
пары, проверку отчёта и границы отката. Compose `WEB_DATA_DIR` можно явно задать
в env file; вложенный `/app/data/current` даёт отсутствующее назначение для legacy
import внутри нового volume. В актуальной Docker-репетиции используется именно
этот путь. Рабочее переключение ожидает подтверждённый рабочий хост и его историю.
