# Развёртывание QA Job Searcher на AWS

21 ноября 2026: для нового AWS experience выбран регион `eu-north-1` и
минимальная стоимость. Конкретная подготовленная конфигурация, расчёт credits
и порядок переноса находятся в [плане запуска](aws-launch-review.md).
Фактический перенос на EC2 с сохранением ngrok выполнен; действующие ресурсы,
команды эксплуатации и ограничения описаны в [итоговом отчёте](aws-migration-status.md).
Примеры Frankfurt и `t3.large` ниже относятся к прежнему предложению;
их не следует применять к текущему проекту без сверки с новым планом.

Инструкция для переноса действующей многопользовательской установки с Mac на AWS.
Базовая схема: один EC2 с Ubuntu, Docker Compose, PostgreSQL 17, Caddy с HTTPS
и приватные резервные копии в S3. После переключения сайт работает независимо
от включённого Mac. OpenAI остаётся внешним платным API.

Актуальность: 15 ноября 2026. Команды приложения сверены с текущим release;
ниже сохранён первоначальный вариант с Caddy. Фактическое развёртывание
21 ноября использует ngrok, SSM и eu-north-1 согласно итоговому отчёту выше.

## 1 Какую версию переносить

Рабочие исходники находятся здесь:

```text
/Users/k.plokhoy/Documents/ChatGPT/QA Job Searcher/.runtime/multiuser-stage10/qa-job-searcher-multiuser-stage10-2026-11-15/qa-job-searcher/
```

Именно там находятся `Dockerfile.runtime`, `compose.runtime.yaml`, PostgreSQL-код,
личные кабинеты и актуальные исправления загрузки источников. Исходники в корне
проекта и `compose.vps.yml` относятся к прежней версии. Их запуск не переносит
текущие аккаунты и историю. Обычный checkout существующего Git HEAD также
не включает все локальные исправления release.

Текущее состояние и результаты локальных проверок:
[отчёт этапа 10](stage10-local-preflight.md). Сохраняется пилот на два места;
перенос хостинга не подтверждает проверку реальной пары и не расширяет пилот.

Для существующей установки выполняй основной путь через backup и restore.
Первоначальная установка без истории описана отдельно в разделе 10.

## 2 Что будет работать на AWS

| Компонент | Где работает | Назначение |
| --- | --- | --- |
| Caddy | Сервис Ubuntu | Публичные HTTP/HTTPS, TLS и проксирование |
| API и React | Контейнер `api` | Интерфейс, авторизация, API и скачивание |
| PostgreSQL 17 | Контейнер `postgres` | Аккаунты, история, задачи и состояния |
| Scheduler | Контейнер `scheduler` | Постановка фоновых заданий |
| Три workers | Отдельные контейнеры | Поиск, диалог и документы |
| MCP | Локальный stdio внутри приложения | Доступ к данным в границах задания |
| Приватные файлы | Docker volume на зашифрованном EBS | Шаблоны, документы и настройки |
| Резервные копии | Локальный volume и закрытый S3 | Восстановление базы вместе с файлами |
| OpenAI | Внешний API по HTTPS | Генерация и независимые проверки |
| Telegram | Дополнительный выключенный profile | Персональный polling при необходимости |

Путь запроса: браузер → HTTPS на Caddy → `127.0.0.1:8000` → API.
PostgreSQL и MCP не публикуются в интернет. Frontend собирается в Docker image
и раздаётся API; отдельный Node-сервер не нужен.

Для текущего пилота предлагается `t3.large`, x86_64, 2 vCPU и 8 GiB RAM,
с 50 GiB зашифрованного `gp3`. Это стартовый размер с запасом для сборки и
рендера, а не измеренная нагрузочная ёмкость. Уменьшать размер лучше после
наблюдения за памятью, CPU и очередью. GPU не требуется.

Один EC2 означает простой при отказе или обслуживании хоста. Эта схема
не обеспечивает высокой доступности. Для неё потребуется отдельный проект
с RDS, несколькими экземплярами и общим хранилищем файлов.

## 3 Что подготовить и сколько это стоит

Понадобятся AWS-аккаунт с оплатой, доступ администратора инфраструктуры,
собственный домен или поддомен, SSH-ключ и доступ к работающему Docker на Mac.
В примерах используются:

| Значение | Пример |
| --- | --- |
| Регион | `eu-central-1`, Frankfurt |
| Домен сайта | `jobs.example.com` |
| Каталог на EC2 | `/srv/qa-job-searcher` |
| Compose project на EC2 | `job-searcher-aws` |
| Data root внутри контейнера | `/app/data/current` |
| SSH-ключ на Mac | `~/.ssh/qa-job-searcher-aws.pem` |

Замени пример домена своим во всех командах и настройках. `EC2_IP` ниже —
переменная с фактическим Elastic IP, а не буквальное имя сервера.

Перед созданием ресурсов посчитай в [AWS Pricing Calculator](https://calculator.aws/)
EC2 Linux On-Demand в Frankfurt, 730 часов в месяц, 50 GiB gp3, один public IPv4,
S3, snapshots, исходящий трафик и при использовании Route 53 — hosted zone.
Для T3 учитывай возможную плату за CPU credits в Unlimited.
OpenAI и регистрация домена оплачиваются отдельно.
[Правила T3 Unlimited](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/burstable-performance-instances-unlimited-mode.html).

Public IPv4 оплачивается также при привязанном Elastic IP. По текущему базовому
тарифу AWS `$0.005/час` один адрес за 730 часов даёт `$3.65` до налогов.
Остальные составляющие зависят от региона, объёма и режима использования.
[Тарифы VPC](https://aws.amazon.com/vpc/pricing/),
[тарифы EC2](https://aws.amazon.com/ec2/pricing/on-demand/).

В Billing → Budgets создай месячный бюджет, например `$100`, с уведомлениями
при 50%, 80% и 100% фактического расхода и при прогнозе выше бюджета.
Подтверди адрес получателя, если AWS прислал verification email.
Уведомление само по себе не выключает ресурсы и не является жёстким лимитом.
[AWS Budgets](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-managing-costs.html).

## 4 Создать EC2 и настроить сеть

Включи MFA для root AWS и используй отдельную операторскую identity или IAM
Identity Center для повседневного управления. Root credentials и постоянные
администраторские access keys на сервер не копируются.

В AWS Console выбери Frankfurt и открой EC2 → Launch instance:

1. Name: `qa-job-searcher`.
2. AMI: официальная Ubuntu Server 24.04 LTS от Canonical, архитектура x86_64.
3. Instance type: `t3.large`; покупка On-Demand.
4. Key pair: создай отдельный ключ, скачай PEM и сохрани его приватно.
5. Network: default VPC и публичная subnet с маршрутом `0.0.0.0/0` через
   Internet Gateway. При отсутствии default VPC создай VPC, subnet, Internet
   Gateway и привязанную route table. Включи DNS resolution.
6. Включи public IPv4 для первоначального доступа.
7. Storage: 50 GiB gp3, Encryption включён. Для сохранения диска при случайном
   удалении EC2 выключи `Delete on termination` у диска с Docker-данными.
8. Advanced details → Metadata: IMDSv2 required, hop limit 1. Секреты в User data
   не размещай. Включи termination protection.

Создай Security Group с такими входящими правилами:

| Протокол | Порт | Источник |
| --- | --- | --- |
| TCP | 22 | Только твой текущий публичный IPv4 с `/32` |
| TCP | 80 | `0.0.0.0/0` |
| TCP | 443 | `0.0.0.0/0` |

Не добавляй входящие правила для 5432, 8000 и MCP. Исходящий доступ оставь
разрешённым: источники вакансий используют разные публичные адреса;
нужны DNS и HTTPS, при сборке также репозитории пакетов.
IPv6 здесь не настраивается; не создавай DNS AAAA для этого сервера.

Шифрование EBS защищает хранилище и использует AWS KMS;
оно не заменяет разграничение доступа к серверу.
[EBS encryption](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-encryption.html).
User data и metadata не предназначены для долговременных секретов.
[EC2 metadata](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-instance-metadata.html).

После запуска открой Elastic IPs → Allocate Elastic IP → Associate Elastic IP
с этим EC2. Запиши адрес. После привязки используй его для SSH и DNS.

На Mac:

```bash
chmod 600 ~/.ssh/qa-job-searcher-aws.pem
EC2_IP='ВСТАВЬ_ELASTIC_IP'
ssh -i ~/.ssh/qa-job-searcher-aws.pem ubuntu@"$EC2_IP"
```

При первом подключении сверь fingerprint SSH host key с данными доверенного
канала, например EC2 system log. Не отключай проверку host key.
Если IP домашнего подключения изменился, обнови `/32` в Security Group.

В качестве второго канала управления можно подключить Session Manager:
нужны работающий SSM Agent, instance profile с `AmazonSSMManagedInstanceCore`
и права оператора. После проверки такого доступа порт 22 можно закрыть;
приведённый ниже SCP тогда потребует SSH через SSM или отдельного канала передачи.
[Требования Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-prerequisites.html).

## 5 Подготовить Ubuntu и Docker

Следующие команды выполняются **на EC2**, от пользователя `ubuntu`, в Bash.
Если любая команда завершается ошибкой, сначала исправь её, затем продолжай.
Системные обновления и перезагрузку выполни до переноса рабочей базы.

```bash
sudo apt-get update
sudo apt-get upgrade -y
sudo apt-get install -y ca-certificates curl gnupg python3 unzip
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<'EOF'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker version
sudo docker compose version
```

Используется официальный apt-репозиторий Docker для Ubuntu 24.04/amd64.
Команды Docker ниже выполняются через `sudo`; добавление пользователя в группу
`docker` не требуется. Docker-публикация портов может обходить правила UFW,
поэтому сохраняй loopback bind API и ограничения Security Group.
[Установка Docker](https://docs.docker.com/engine/install/ubuntu/).

Для ограничения размера Docker-логов на **новом сервере** создай
`/etc/docker/daemon.json`:

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "log-driver": "local",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
EOF
sudo systemctl restart docker
sudo install -d -m 0700 -o ubuntu -g ubuntu /srv/qa-job-searcher
```

Если `daemon.json` уже содержит настройки, объедини JSON, сохраняя действующие
поля. Этот restart выполняется до запуска приложения.
[Docker local logging driver](https://docs.docker.com/engine/logging/drivers/local/).

## 6 Передать актуальные исходники

**На Mac** создай отдельный архив из release. Список включений исключает
рабочие данные, `.runtime`, виртуальное окружение и конфиденциальные настройки.
Перед упаковкой проверь результаты полного quality gate для этих исходников;
команды и требования приведены в разделе 16.

```bash
PROJECT_DIR='/Users/k.plokhoy/Documents/ChatGPT/QA Job Searcher'
RELEASE_DIR="$PROJECT_DIR/.runtime/multiuser-stage10/qa-job-searcher-multiuser-stage10-2026-11-15/qa-job-searcher"
EXPORT_DIR="$PROJECT_DIR/.runtime/aws-transfer"
umask 077
mkdir -p "$EXPORT_DIR"
COPYFILE_DISABLE=1 tar \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.DS_Store' \
  --exclude='node_modules' \
  --exclude='dist' \
  --exclude='coverage' \
  --exclude='playwright-report' \
  --exclude='test-results' \
  -czf "$EXPORT_DIR/source.tar.gz" \
  -C "$RELEASE_DIR" \
  AGENTS.md README.md pyproject.toml requirements.lock \
  Dockerfile.runtime compose.runtime.yaml compose.test.yaml \
  .dockerignore .gitignore .env.example config src tests frontend docs
shasum -a 256 "$EXPORT_DIR/source.tar.gz"
tar -tzf "$EXPORT_DIR/source.tar.gz"
```

Перед отправкой проверь список: в архиве не должно быть `.env`, `.runtime`,
SQLite, личных CV, ключей и рабочих документов. Передавай только проверенные
собственные исходники; не подставляй архив от неизвестного источника.

```bash
scp -i ~/.ssh/qa-job-searcher-aws.pem \
  "$EXPORT_DIR/source.tar.gz" ubuntu@"$EC2_IP":/srv/qa-job-searcher/
```

**На EC2** сверь SHA-256 с Mac и распакуй архив:

```bash
cd /srv/qa-job-searcher
sha256sum source.tar.gz
tar -xzf source.tar.gz
install -d -m 0700 .runtime
```

Образ собирается непосредственно на EC2 под amd64. Образ с Apple Silicon
может быть arm64; его нельзя считать взаимозаменяемым с amd64 по локальному image ID.
Не используй image ID Mac как digest готового образа для EC2.

## 7 Создать приватную конфигурацию на EC2

**На EC2**, в `/srv/qa-job-searcher`, создай файл настроек без вывода пароля
в терминал. Сначала замени домен в примере:

```bash
python3 - <<'PY'
import os
import secrets
from pathlib import Path

path = Path('.runtime/runtime.env')
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, 'w') as stream:
    stream.write(f'POSTGRES_PASSWORD={secrets.token_hex(32)}\n')
    stream.write('POSTGRES_DB=job_searcher\n')
    stream.write('WEB_PORT=8000\n')
    stream.write('WEB_ORIGINS=https://jobs.example.com\n')
    stream.write('WEB_DATA_DIR=/app/data/current\n')
    stream.write('OPENAI_USE_ENV_PROXY=0\n')
PY
```

`O_EXCL` не даёт случайно перезаписать существующий пароль. Не повторяй создание
env при каждом запуске: смена переменной сама по себе не меняет пароль уже
инициализированной PostgreSQL. Для PostgreSQL используется случайная hex-строка,
поскольку пароль входит в URL подключения.

Задай функцию для остальных команд **в этой SSH-сессии**:

```bash
dc() {
  sudo docker compose -p job-searcher-aws \
    --env-file .runtime/runtime.env -f compose.runtime.yaml "$@"
}
dc config --quiet
dc build
dc up -d --wait postgres
```

`dc config --quiet` проверяет конфигурацию без печати развёрнутого DSN.
Обычный `docker compose config` выводит секреты, подставленные в environment.
После нового SSH-входа вернись в этот каталог и объяви `dc` заново.

При переносе истории на этом шаге запускается **только PostgreSQL**.
Не запускай `migrate`, bootstrap, API или workers до restore: назначение должно
оставаться пустым. На EC2 пока нет пользовательских данных.

## 8 Получить согласованную копию с Mac

Подготовь сервер и сборку заранее. Следующий шаг начинает окно обслуживания;
Mac остаётся остановленным до решения о переключении.

**На Mac** настрой функцию именно для действующего deployment:

```bash
cd "$RELEASE_DIR"
dmac() {
  docker compose -p job-searcher-stage10 \
    --env-file "$PROJECT_DIR/.runtime/stage10-runtime.env" \
    -f compose.runtime.yaml \
    -f "$PROJECT_DIR/.runtime/stage10-compose.override.json" "$@"
}
dmac ps
```

Эти имена подтверждены отчётом от 15 ноября. Если установка после этого менялась,
сначала установи фактические project/env/override; другой project может создать
пустые volumes. Рабочую SQLite и старые launchd API/worker не запускай.

Через административную диагностику проверь активные задания и незавершённые
платные вызовы. Дай начатым заданиям завершиться или штатно отмени их; неизвестный
исход OpenAI сохрани для разбора, без автоматической повторной генерации.

```bash
dmac run --rm maintenance python -m qa_job_bot.maintenance on --data-dir /app/data/current
dmac --profile telegram stop api scheduler search-worker chat-worker document-worker telegram
dmac ps -a
BACKUP_NAME="aws-move-$(date -u +%Y%m%dT%H%M%SZ)"
dmac run --rm maintenance python -m qa_job_bot.runtime_backup backup \
  "/backups/$BACKUP_NAME" --services-stopped
dmac run --rm -T maintenance tar -C /backups -czf - "$BACKUP_NAME" \
  > "$EXPORT_DIR/postgres-and-files.tar.gz"
chmod 600 "$EXPORT_DIR/postgres-and-files.tar.gz"
shasum -a 256 "$EXPORT_DIR/postgres-and-files.tar.gz"
```

`--services-stopped` — подтверждение оператора, а не команда остановки.
Если утилита сообщает о действующей lease, дождись её истечения и повтори backup
в новое отсутствующее назначение. Не обходи проверку. PostgreSQL остаётся запущенным,
все прикладные писатели, включая Telegram и внешние скрипты, остановлены.

Архив содержит `manifest.json`, `postgres.dump` и полное дерево `files`.
Один SQL dump не сохраняет шаблоны, документы и файловые настройки.
Копия приватная; не загружай её в GitHub, публичную ссылку или общие CI artifacts.
Действующие runtime env и override сохрани отдельно в приватной операторской копии.

```bash
scp -i ~/.ssh/qa-job-searcher-aws.pem \
  "$EXPORT_DIR/postgres-and-files.tar.gz" \
  ubuntu@"$EC2_IP":/srv/qa-job-searcher/.runtime/
```

Запиши значение `BACKUP_NAME`: оно понадобится на EC2.
Mac пока не возобновляй. Параллельная работа двух копий создаёт расходящуюся историю,
дубли поиска и потенциально повторные платные вызовы.

## 9 Восстановить историю на EC2

**На EC2** сверь SHA-256 архива с Mac, затем проверь состав:

```bash
cd /srv/qa-job-searcher
sha256sum .runtime/postgres-and-files.tar.gz
tar -tzf .runtime/postgres-and-files.tar.gz
BACKUP_NAME='ВСТАВЬ_ИМЯ_aws-move_С_MAC'
```

Внутри должен быть один каталог с записанным `BACKUP_NAME`, без абсолютных путей
и `..`. Распаковывай только собственную копию с совпавшим SHA-256.

Передай архив в backups volume через maintenance-контейнер:

```bash
dc run --rm -T maintenance tar -xzf - -C /backups \
  < .runtime/postgres-and-files.tar.gz
dc run --rm maintenance python -m qa_job_bot.runtime_backup restore \
  "/backups/$BACKUP_NAME" --services-stopped
dc run --rm maintenance python -m qa_job_bot.maintenance on --data-dir /app/data/current
```

Restore проверяет revision/schema, SHA-256 dump и файлов и требует пустые БД
и data directory. Абсолютный путь должен совпадать с исходным:
`WEB_DATA_DIR=/app/data/current`. Нельзя заменить его на `/app/data` ради удобства:
пути к артефактам сохранены в БД.

Копия переносит аккаунты, хеши паролей, историю и пилотное ограничение вместе с БД.
Повторный bootstrap, legacy import SQLite и `pilot begin` после restore не нужны.
Они относятся к другим сценариям.

Если restore сообщил, что назначение непустое, остановись. Подготовь отдельный
пустой Compose project с отдельными volumes или новый пустой сервер. Не очищай
рабочие volumes и не удаляй данные, чтобы пройти эту проверку.

После restore проверь серверную OpenAI-конфигурацию без вывода её содержимого:

```bash
dc run --rm maintenance python -c 'from pathlib import Path; from qa_job_bot.openai_config import OpenAISettings; OpenAISettings.load(Path("/app/data/current")); print("OpenAI configuration: valid")'
```

Если файлов не оказалось в копии, перенеси действующие `openai-api-key` и
`openai-agent.json` отдельно через приватный канал в `/app/data/current`.
Владелец файлов внутри volume — UID/GID `10001:10001`, права 0600;
каталоги — 0700. Сохраняй действующие настройки API и агента.
Ключ не вставляется в frontend, Git, Dockerfile или чат.

Подробный контракт находится в `docs/agent-runtime.md` внутри release.
Поиск без API-конфигурации работает; платные функции при её отсутствии завершаются
ошибкой. Успешный `/health/ready` не доказывает доступность OpenAI.

Дальше переходи к разделу 11.

## 10 Альтернативный путь для пустой установки

Этот раздел выполняется **вместо разделов 8–9**, если сознательно создаётся
новая установка без текущих аккаунтов и истории.

После шага 7:

```bash
dc run --rm migrate
dc run --rm maintenance qa-job-import bootstrap --login owner --display-name Owner
dc run --rm maintenance python -m qa_job_bot.maintenance on --data-dir /app/data/current
```

После настройки HTTPS и запуска из раздела 11 выключи обслуживание, открой сайт
и установи пароль владельца через штатную форму. Setup token прочитай только
в приватном терминале:

```bash
dc exec api cat /app/data/current/setup-token
```

Новый пустой deployment не наследует ограничения пилота действующей базы.
Приглашения и режим допуска настраиваются отдельно по требованиям проекта.
Для генерации также потребуется приватно установить серверную OpenAI-конфигурацию.
Существующие SQLite и PostgreSQL при таком запуске не импортируются автоматически.

## 11 Подключить домен и HTTPS

В DNS у текущего регистратора создай запись `A` для `jobs.example.com`
со значением Elastic IP. На время переключения поставь TTL 300 секунд.
Если DNS обслуживается Route 53, запись создаётся в hosted zone домена;
для переноса DNS к AWS отдельно делегируются NS у регистратора.
Покупать домен и переносить DNS в Route 53 для этой схемы необязательно.

**На EC2** установи Caddy из официального репозитория:

```bash
sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
sudo chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
sudo chmod o+r /etc/apt/sources.list.d/caddy-stable.list
sudo apt-get update
sudo apt-get install -y caddy
```

Пакет устанавливает systemd-сервис Caddy.
[Установка Caddy](https://caddyserver.com/docs/install).

Создай `/etc/caddy/Caddyfile`, заменив домен и почту:

```bash
sudo tee /etc/caddy/Caddyfile >/dev/null <<'EOF'
{
    email admin@example.com
}

jobs.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000 {
        flush_interval -1
    }
}
EOF
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl enable --now caddy
sudo systemctl reload caddy
```

`flush_interval -1` позволяет передавать события интерфейса без накопления
ответа прокси. Caddy сохраняет Host, с которым приложение проверяет допустимый
адрес и устанавливает Secure cookie. Домен в Caddyfile и `WEB_ORIGINS` должен
совпадать. Не добавляй произвольные origins и не отключай CSRF для устранения 403.

Caddy получает и обновляет сертификат автоматически и перенаправляет HTTP
на HTTPS. Для выбранного способа настройки нужны корректный DNS и доступные
80/443. Отдельный ACM-сертификат и ALB не используются.
[Automatic HTTPS](https://caddyserver.com/docs/automatic-https).

Приложение после restore пока в обслуживании:

```bash
dc up -d
dc ps -a
curl -sS -o /dev/null -w '%{http_code}\n' https://jobs.example.com/health/live
curl -sS -o /dev/null -w '%{http_code}\n' https://jobs.example.com/health/ready
```

Ожидается live 200 и ready 503, пока marker обслуживания включён.
На этом шаге не используй `up --wait`: API намеренно не проходит readiness.
До запуска backend Caddy может возвращать 502.

## 12 Проверить AWS и открыть доступ

При необходимости проверь настоящие OpenAI и MCP до открытия доступа.
Следующая команда выполняет **платные API-вызовы** с синтетическими аккаунтами
и документами; она не является частью healthcheck:

```bash
dc run --rm -e SMOKE_REPORT_DIR=/backups/aws-live-smoke maintenance \
  python -m qa_job_bot.live_smoke --credentials-dir /app/data/current
```

Smoke использует отдельную временную PostgreSQL schema и проверяет генерацию,
независимые проверки, скачивание своих файлов, запрет чужого доступа и отказ
для неподтверждённого опыта. Приватный отчёт сохраняется в backups volume.
Ошибка не обходится отключением reviewer. Счёт провайдера проверяется отдельно;
неизвестный usage не считается нулевым.

Для открытия приложения:

```bash
dc run --rm maintenance python -m qa_job_bot.maintenance off --data-dir /app/data/current
dc up -d --wait
dc ps -a
curl -sS -o /dev/null -w '%{http_code}\n' https://jobs.example.com/health/ready
curl -sS https://jobs.example.com/api/session
curl -sS -o /dev/null -w '%{http_code}\n' https://jobs.example.com/api/jobs
curl -sS -o /dev/null -w '%{http_code}\n' https://jobs.example.com/api/profile
```

Ожидается:

| Проверка | Результат |
| --- | --- |
| PostgreSQL, API, scheduler и три workers | Шесть healthy-служб |
| `migrate` | `Exited (0)` после одноразового запуска |
| `/health/ready` | 200 |
| `/api/session` без cookie | `account_mode: true`, пользователь не авторизован |
| `/api/jobs` и `/api/profile` без cookie | 401 |
| Открытие сайта | HTTPS без ошибок сертификата |

Если обновление завершилось с ошибкой, не считай частично работающий набор служб
успешным запуском.

В браузере войди прежним логином и паролем. Проверь свою историю, Applied/архив,
профиль, источники, доступные шаблоны, диалоги и документы, если они были в исходной
базе. Сделай поиск, обнови страницу, проверь сохранение результата и ошибки
отдельных источников. Для документов проверь PDF preview и DOCX/PDF download,
включая фактическую генерацию с действующим API.

Второй активированный аккаунт проверяй в другом профиле браузера: его профиль,
статусы и документы должны быть независимы. Чужие task/artifact/conversation IDs
не должны давать содержимое по прямому API-запросу. Pending-приглашение второго
участника не заменяет такую проверку. Ссылки приглашений со старым ngrok-доменом
обновляй через штатное управление приглашениями для нового домена.

После первого успешного offline backup и проверки восстановления выполни
контролируемую перезагрузку EC2. Повторно проверь шесть healthy-служб, HTTPS,
вход и сохранность данных. `restart: unless-stopped` поднимает запущенные ранее
контейнеры после запуска Docker; вручную остановленные контейнеры сами не стартуют.
Caddy и Docker должны быть enabled в systemd.

## 13 Завершить переключение с Mac

После принятия AWS-установки используй новый домен как основной адрес.
Проверь и останови на Mac фактический `com.qajobsearcher.tunnel`, обслуживающий
прежний ngrok URL, средствами launchd. API/scheduler/workers исходного Compose
остаются остановленными. Не удаляй исходные volumes, SQLite и приватные копии.

Отдельно проверь контроллер автозапуска: его запуск не должен снова поднимать
старый Compose с писателями. Перезагрузка Mac не должна создавать второй runtime.

После открытия AWS scheduler и действия пользователей уже могут добавлять данные.
С этого момента простой старт старой копии на Mac теряет новый прогресс.
Возврат требует нового offline backup с AWS и восстановления в отдельное
назначение с проверкой данных. Не возвращай трафик на устаревшую БД.

## 14 Настроить S3 для резервных копий

В S3 создай отдельный bucket в Frankfurt, например
`qa-job-searcher-backups-ACCOUNT_ID`. Имя bucket должно быть уникальным.

Включи все настройки Block Public Access, Object Ownership `Bucket owner enforced`,
Versioning и Default encryption SSE-S3. Доступ предоставь только оператору
и роли сервера. Добавь bucket policy, запрещающую транспорт без TLS.
Не включай static website hosting и не выдавай публичные ссылки на копии.
[S3 Block Public Access](https://docs.aws.amazon.com/AmazonS3/latest/userguide/access-control-block-public-access.html).

Назначь EC2 instance role с правом записи только в `runtime/` этого bucket.
Для этого в IAM → Roles → Create role выбери доверенную службу EC2, добавь
отдельную policy ниже, затем в EC2 → Actions → Security → Modify IAM role
прикрепи роль к серверу. Если используется SSM, сохрани необходимые ему права.
Для простой однокомандной загрузки достаточно такой identity policy,
после замены имени bucket:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:AbortMultipartUpload"],
      "Resource": "arn:aws:s3:::qa-job-searcher-backups-ACCOUNT_ID/runtime/*"
    }
  ]
}
```

Для восстановления оператору нужны отдельные `s3:GetObject` и, при выборе
из списка, `s3:ListBucket` с соответствующим ограничением prefix.
Роль сервера не получает удаление резервных копий. Для SSE-KMS вместо SSE-S3
дополнительно нужны подходящие IAM и KMS key policy.
Постоянные AWS access keys в `.env` не нужны: AWS CLI на хосте получает временные
credentials instance role. Роль не требуется передавать контейнерам приложения.

Установи AWS CLI v2 на EC2:

```bash
curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip -o /tmp/qa-awscliv2.zip
unzip -q /tmp/qa-awscliv2.zip -d /tmp/qa-awscli-install
sudo /tmp/qa-awscli-install/aws/install
aws --version
aws sts get-caller-identity
```

Для повторной установки используй предусмотренный AWS режим обновления.
[Установка AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html).

Сделай первую копию **на EC2** в окне обслуживания:

```bash
umask 077
install -d -m 0700 .runtime/exports
BACKUP_NAME="aws-backup-$(date -u +%Y%m%dT%H%M%SZ)"
dc run --rm maintenance python -m qa_job_bot.maintenance on --data-dir /app/data/current
dc --profile telegram stop api scheduler search-worker chat-worker document-worker telegram
dc run --rm maintenance python -m qa_job_bot.runtime_backup backup \
  "/backups/$BACKUP_NAME" --services-stopped
dc run --rm -T maintenance tar -C /backups -czf - "$BACKUP_NAME" \
  > ".runtime/exports/$BACKUP_NAME.tar.gz"
sha256sum ".runtime/exports/$BACKUP_NAME.tar.gz" \
  > ".runtime/exports/$BACKUP_NAME.tar.gz.sha256"
dc run --rm maintenance python -m qa_job_bot.maintenance off --data-dir /app/data/current
dc up -d --wait
```

После успешного backup приложение можно возобновить до отправки копии в S3.
Если команда не прошла, сохрани причину ошибки и явно реши, можно ли возобновить
сервисы; не продолжай цепочку так, будто копия готова.

```bash
BACKUP_BUCKET='qa-job-searcher-backups-ACCOUNT_ID'
aws s3 cp ".runtime/exports/$BACKUP_NAME.tar.gz" \
  "s3://$BACKUP_BUCKET/runtime/$BACKUP_NAME.tar.gz" --sse AES256
aws s3 cp ".runtime/exports/$BACKUP_NAME.tar.gz.sha256" \
  "s3://$BACKUP_BUCKET/runtime/$BACKUP_NAME.tar.gz.sha256" --sse AES256
```

Если Telegram ранее работал, верни его отдельно только после запуска остальных
служб: `dc --profile telegram up -d --wait telegram`.
Обычный `dc up` этот необязательный profile не включает.

Резервируй данные ежедневно и перед обновлением; это предполагает короткое
окно обслуживания, поскольку штатный backup пока offline. Для автоматического
запуска подготовь systemd timer с тем же порядком, блокировкой параллельного
запуска и уведомлением при ошибке. Одна загрузка в S3 не включает расписание.
До настройки и проверки timer копии выполняются оператором.

Выбери retention, например ежедневные копии за 14 дней и месячные за 3 месяца.
В Lifecycle учти current и noncurrent versions и незавершённые multipart uploads;
одна expiration policy для текущих объектов не очищает все старые версии.
Сначала проверь восстановление, затем настрой удаление старых копий.

S3 хранит offsite backup; приложение продолжает читать документы из volume.
Для прямого хранения рабочих документов в S3 потребуется изменение приложения.
EBS snapshots полезны дополнительно. Для согласованности AWS рекомендует
приостанавливать запись перед snapshot; они не заменяют проверенный переносимый
backup БД вместе с файлами.
[Создание EBS snapshots](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-creating-snapshot.html).

## 15 Проверить восстановление и подготовить откат

Скачай одну S3-копию и файл SHA-256 от имени оператора с правом чтения.
Например, через AWS Console сохрани их локально и передай по SCP в
`/srv/qa-job-searcher/.runtime/exports/`. На EC2 перейди в `/srv/qa-job-searcher` и
проверь SHA-256 командой `sha256sum -c .runtime/exports/ИМЯ_АРХИВА.tar.gz.sha256`:
в checksum-файле сохранён путь относительно корня проекта.
Команды загрузки могут работать и через AWS CLI с отдельно выданным правом чтения.

Для первой репетиции **на EC2** подготовь отдельный env. Замени recovery-домен
в Python ниже, заранее направив его на Elastic IP. Порт 8001 должен быть свободен.

```bash
cd /srv/qa-job-searcher
python3 - <<'PY'
import os
import secrets
from pathlib import Path

updates = {
    'POSTGRES_PASSWORD': secrets.token_hex(32),
    'POSTGRES_DB': 'job_searcher_recovery',
    'WEB_PORT': '8001',
    'WEB_ORIGINS': 'https://recovery-jobs.example.com',
}
descriptor = os.open('.runtime/recovery.env', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, 'w') as stream:
    for line in Path('.runtime/runtime.env').read_text().splitlines():
        name, value = line.split('=', 1)
        stream.write(f'{name}={updates.get(name, value)}\n')
PY
drec() {
  sudo docker compose -p job-searcher-aws-recovery \
    --env-file .runtime/recovery.env -f compose.runtime.yaml "$@"
}
BACKUP_NAME='ВСТАВЬ_ИМЯ_КОПИИ'
drec up -d --wait postgres
drec run --rm -T maintenance tar -xzf - -C /backups \
  < ".runtime/exports/$BACKUP_NAME.tar.gz"
drec run --rm maintenance python -m qa_job_bot.runtime_backup restore \
  "/backups/$BACKUP_NAME" --services-stopped
drec run --rm maintenance python -m qa_job_bot.maintenance on --data-dir /app/data/current
drec up -d api
```

Не запускай миграции до restore и не подключай рабочие volumes. Эта репетиция
использует отдельные `job-searcher-aws-recovery_*` volumes.
При повторной репетиции выбери новый project name и env: существующее
назначение уже непустое. `WEB_DATA_DIR` остаётся `/app/data/current`.

Добавь в существующий Caddyfile второй site block, сохранив основной:

```text
recovery-jobs.example.com {
    reverse_proxy 127.0.0.1:8001 {
        flush_interval -1
    }
}
```

Проверь конфигурацию `caddy validate` и выполни `systemctl reload caddy`
с `sudo`, как в разделе 11. Затем выключи обслуживание **только recovery**:

```bash
drec run --rm maintenance python -m qa_job_bot.maintenance off --data-dir /app/data/current
drec up -d --wait api
```

Scheduler, workers и Telegram в recovery не запускай: это исключает выполнение
восстановленных фоновых задач при проверке входа и имеющихся файлов.
Рабочий домен и основной deployment продолжают работать отдельно.

Проверь manifest, файлы и вход, сравни количество значимых записей с источником,
профиль, историю, Applied/архив, шаблоны, PDF preview/download и запрет доступа
чужого аккаунта. Пустые в источнике сущности не объявляй проверенными на реальных
данных: для них нужны синтетические проверки или данные пилота.

После проверки останови recovery через `drec stop` и убери его временный
site block из Caddy с проверкой и reload. Не применяй к основному project команды
очистки. Копию и результаты репетиции сохрани приватно.

Восстановление не является общей транзакцией БД и файлов. Если процесс оборвался
между восстановлением базы и публикацией файлов, оставь службы остановленными
и повтори restore в новое пустое назначение, сохранив исходную копию.

При аварии рабочего EC2 порядок такой: остановить старых писателей → подготовить
новый EC2 → установить ту же проверенную версию → восстановить полный backup →
проверить → переназначить Elastic IP или DNS → открыть доступ.
Размер возможной потери данных определяется возрастом последней копии;
ежедневный backup сам по себе допускает потерю изменений примерно за сутки.

## 16 Проверки качества и обновления

Для обновления исходников сначала выполни полный gate **в каталоге release**,
в отдельном окружении с тестовой PostgreSQL. Не указывай рабочую БД в
`QA_TEST_DATABASE_URL`.

Пример подготовки тестовой PostgreSQL на машине разработчика, если порт 55432
свободен:

```bash
docker compose -p qa-aws-quality -f compose.test.yaml up -d --wait
QA_TEST_DATABASE_URL='postgresql://job_searcher:local-test-only@127.0.0.1:55432/job_searcher_test' \
  .venv/bin/python -m qa_job_bot.quality
```

Нужны установленное dev-окружение Python, зависимости frontend и Playwright
с браузером по README release. Gate запускает backend unit/integration/API/MCP,
frontend/component, browser E2E, lint, сборку и проверяет четыре отдельных
порога: backend lines/branches и frontend lines/branches не ниже 90%.

В последнем локальном отчёте release за 15 ноября: 1408 backend-тестов,
169 frontend-тестов, 24 browser E2E; backend lines 98.368154%, branches 95.619835%,
frontend lines 99.553571%, branches 95.638945%.
Это измерения той локальной версии, а не результаты AWS и не доказательство
успешного нового развёртывания. Live API проверяется отдельно.

Перед обновлением production создай offline backup и сохрани прежний image ID:

```bash
dc images
sudo docker image inspect job-searcher-runtime:local --format '{{.Id}}'
```

Закрепи прежний image отдельным уникальным rollback tag по полученному ID,
сохрани старые исходники и настройки приватно. Затем передай новые проверенные
исходники, собери образ и выполни обновление в окне обслуживания.
После миграций повтори readiness, вход, поиск и проверки документов.

Откат image не означает откат схемы. Если старая версия несовместима с новой БД,
нужно восстановить согласованную копию в отдельные volumes и сохранить новые
данные для сверки. Не запускай старый image против изменённой схемы наугад.

Публикация GitHub и CI/CD не требуется для описанного ручного переноса. Если
впоследствии создаётся репозиторий, выполни правила первой публикации проекта,
не включай секреты/историю и сделай обязательные проверки блокирующими merge.

## 17 Наблюдение и частые проблемы

В EC2/CloudWatch настрой уведомления о `StatusCheckFailed`, устойчивой загрузке
CPU и исчерпании CPU credits T3. Для памяти и заполнения диска нужен отдельный
CloudWatch Agent или иной мониторинг: стандартные EC2-метрики этого не дают.
[Метрики EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/viewing_metrics_with_cloudwatch.html).
Добавь внешний HTTPS-check `/health/ready` без cookies и личных данных.
Следи также за успешностью backup, возрастом последней копии и очередью задач
в административной диагностике. Readiness API не заменяет heartbeat workers.

Основные команды на EC2:

```bash
dc ps -a
dc logs --tail=100 api scheduler search-worker chat-worker document-worker
sudo journalctl -u caddy --since '30 minutes ago'
df -h
sudo docker system df
```

Логи, env и backup остаются приватными; перед пересылкой диагностики проверяй
её на персональные данные. Не выполняй `docker compose down --volumes`,
`docker volume prune` и очистку неизвестных volumes на рабочем сервере.

| Симптом | Что проверить |
| --- | --- |
| SSH timeout | Elastic IP, маршрут к IGW, EC2 status и текущий `/32` в Security Group |
| HTTPS не получает сертификат | DNS A/AAAA, доступность 80/443, Caddy journal и CAA домена |
| Caddy 502 | Запуск `api` и loopback-порт 8000 |
| 400 или 403 при входе | Совпадение домена, Host и `WEB_ORIGINS`, cookie/CSRF; не отключать защиту |
| Ready 503 | Marker обслуживания, PostgreSQL, revision и завершение `migrate` |
| Workers unhealthy | PostgreSQL, heartbeat, журнал конкретной службы и свободные RAM/диск |
| Restore отклонён | Пустота назначения, совпадение data root/revision/schema и SHA-256 |
| Поиск есть, документы не создаются | OpenAI-файлы, UID/права, доступность API, лимиты и вердикт проверки |
| Задача долго queued | Все три workers, расписание/лимиты, очередь и состояние reservation |
| Один источник дал 429/5xx | Сводка поиска; частичный отказ не требует отключения защит загрузчика |
| После restart нет контейнеров | Правильный project/env, `dc up -d --wait`, enabled Docker и Caddy |
| Повторные Telegram-сообщения | Нет ли polling на Mac или recovery; PostgreSQL lock действует лишь в одной БД |

Для необязательного Telegram сначала проверь принадлежность chat ID аккаунту,
помести приватный `telegram-bot-token` в data root и выполни штатную привязку:

```bash
dc run --rm maintenance python -m qa_job_bot.personal_telegram bind \
  --chat-id 123456789 --login member
dc --profile telegram up -d --wait telegram
```

Заменяй ID и login фактическими данными. Привязка создаётся с автоуведомлениями
off; включение выполняется пользователем. Перед polling старый процесс с этим
токеном должен быть остановлен. Telegram не является условием работы сайта.

Развёртывание считается принятым после успешного restore, HTTPS и проверки
входа, истории, изоляции аккаунтов, поиска, документов с live API, backup/restore
и автозапуска после перезагрузки. Выполненные проверки и конкретные ограничения
записываются отдельно; локальный отчёт не заменяет проверку AWS.
