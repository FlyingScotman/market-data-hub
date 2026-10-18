# Первый деплой на VPS: чеклист

Бандл: `dist/market-data-hub-20261018.tar.gz`, версия **v0.1.0**, коммит `566a93c`.
SHA-256: `5e2b43988fcda8f9e38c30a6d147baceb813863504cba71a704cca3a4bd4a7f0`.
Внутри код, seed-дамп (720 МБ, база 3,6 ГБ, миграции до 0424) и `SEED_REPORT.md`.

Подробности по каждому шагу лежат в [README_DEPLOY.md](README_DEPLOY.md), номера разделов указаны в скобках.
Ставим сразу в раскладку §13, чтобы потом работало самообновление из админки.

> **Это первый запуск на реальном Docker.** Образ, compose и Caddy ещё нигде не собирались, тесты
> шли без Docker. Если шаг падает, сохрани вывод `docker compose logs <service>` и пришли его мне.

---

## 0. Подготовить заранее

- [ ] VPS: Ubuntu 24.04, 4 ГБ RAM, 2–4 vCPU, SSD от 80 ГБ, SSH-доступ по ключу.
- [ ] Два DNS-имени, обе A/AAAA-записи указывают на VPS. Дальше в тексте они записаны как
      `api.example.com` (API) и `admin.example.com` (админка).
      **Имя админки потом лучше не менять**: passkeys привязаны к нему.
- [ ] Свои внешние IP для allow-list админки (офис, дом) в виде CIDR, например `203.0.113.7/32`.
      Если домашний IP динамический, нужна подсеть пошире или WireGuard.
- [ ] E-mail для Let's Encrypt.
- [ ] Ключи источников: `MOEX_API_KEY`, `RATESCALC_*`, `OTC_MONITOR_TOKEN`, при необходимости `CBONDS_*`
      и `MOEX_PASSPORT_*`. **Удобнее вводить их уже в админке (шаг 9)**, тогда в `.env` они не
      попадают совсем.
- [ ] Логин первого админа и устройство с passkey: Touch ID, YubiKey или телефон.

## 1. Скопировать бандл (с Mac)

```bash
scp dist/market-data-hub-20261018.tar.gz dist/market-data-hub-20261018.tar.gz.sha256 vps:/tmp/
```

## 2. Подготовить сервер (§1)

```bash
ssh vps
sudo apt update && sudo apt -y upgrade
# Docker Engine + compose plugin: https://docs.docker.com/engine/install/ubuntu/
docker compose version          # нужна версия 2.24 или новее (env_file с required: false)
sudo ufw default deny incoming && sudo ufw default allow outgoing
sudo ufw allow 22/tcp && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw enable
sudo timedatectl set-ntp true   # время важно: страница Machine проверяет расхождение часов
```

Docker публикует порты в обход ufw. Поэтому compose вешает 5432 и 8080 только на `127.0.0.1`.
Других портов не публикуй.

## 3. Разложить релиз (§13, раскладка)

```bash
V=0.1.0
cd /tmp && sha256sum -c market-data-hub-20261018.tar.gz.sha256
sudo mkdir -p /opt/mdhub/releases /opt/mdhub/archives /opt/mdhub/shared/backups
sudo tar xzf /tmp/market-data-hub-20261018.tar.gz -C /opt/mdhub/releases
sudo mv /opt/mdhub/releases/mdhub /opt/mdhub/releases/$V
cd /opt/mdhub/releases/$V && sudo sha256sum -c --quiet SHA256SUMS && cat MANIFEST.txt | head -5
sudo ln -s releases/$V /opt/mdhub/current
sudo chown 10001:10001 /opt/mdhub/shared/backups && sudo chmod 750 /opt/mdhub/shared/backups
sudo cp /opt/mdhub/current/deploy/env.example /opt/mdhub/shared/.env
sudo chmod 600 /opt/mdhub/shared/.env
sudo ln -s /opt/mdhub/shared/.env /opt/mdhub/current/deploy/.env
```

## 4. Заполнить `.env` (`sudoedit /opt/mdhub/shared/.env`)

Секреты лучше сгенерировать сразу одним блоком и вставить вывод в файл:

```bash
for v in POSTGRES_PASSWORD MDHUB_RO_PASSWORD MDHUB_API_DB_PASSWORD MDHUB_ADMIN_DB_PASSWORD MDHUB_HOSTMON_DB_PASSWORD; do
  echo "$v=$(openssl rand -hex 24)"; done
echo "MDHUB_ADMIN_SECRET_KEY=$(openssl rand -hex 32)"
```

### Обязательные

| Переменная | Значение |
|---|---|
| `POSTGRES_DB` | `mdhub` |
| `POSTGRES_USER` | `mdhub` |
| `POSTGRES_PASSWORD` | сгенерированное значение (hex, без спецсимволов: подставляется в URL) |
| `MDHUB_RO_USER` | `mdhub_ro` |
| `MDHUB_RO_PASSWORD` | сгенерированное значение. Если оставить пустым, роль создастся без права входа |
| `MDHUB_API_DB_PASSWORD` | сгенерированное значение: api работает под своей ролью `mdhub_api` (§16) |
| `MDHUB_ADMIN_DB_PASSWORD` | сгенерированное значение: роль `mdhub_admin` |
| `MDHUB_HOSTMON_DB_PASSWORD` | сгенерированное значение: роль `mdhub_hostmon` |
| `MDHUB_BACKUP_HOST_DIR` | `/opt/mdhub/shared/backups` |
| `MDHUB_IMAGE_TAG` | `0.1.0` (обязательно, этот тег использует агент обновлений) |
| `MDHUB_API_DOMAIN` | `api.example.com` |
| `ACME_EMAIL` | твой e-mail |
| `MDHUB_ADMIN_DOMAIN` | `admin.example.com` |
| `MDHUB_ADMIN_ALLOW_CIDRS` | `"203.0.113.7/32 198.51.100.0/24"`: через пробел, в кавычках. **Если пусто, админка отказывает всем** |
| `MDHUB_ADMIN_SECRET_KEY` | сгенерированное значение (32 байта hex) |
| `MDHUB_SECRETS_PRIVATE_KEY` | пока пусто, заполняется на шаге 5 |

Три пароля сервисных ролей нужно задать **до** первого старта postgres. Тогда initdb сам создаст
роли. Если их не задать, api, админка и hostmon будут работать под владельцем и писать
предупреждение в лог.

### Оставить пустыми, их выставляет compose

`MDHUB_DATABASE_URL`, `MDHUB_BACKUP_DIR`, `MDHUB_ADMIN_TRUSTED_PROXIES` (адрес Caddy фиксирован в
compose), `MDHUB_ADMIN_ORIGIN` (по умолчанию `https://<MDHUB_ADMIN_DOMAIN>`).

### Ключи источников: в `.env` или в админке

`MOEX_API_KEY`, `MOEX_PASSPORT_LOGIN/PASSWORD`, `RATESCALC_USERNAME/PASSWORD`,
`CBONDS_LOGIN/PASSWORD`, `OTC_MONITOR_TOKEN`, `MDHUB_HTTP_PROXY`. Рекомендую оставить их пустыми и
ввести на шаге 9. Пока ключа нет, задача соответствующего источника пропускается (skipped), а
публичные ISS и CBR работают и без ключей.

### Токены API

`MDHUB_API_TOKENS` оставь пустым. Именованные токены потребителей выпускаются на шаге 7 или на
странице Tokens.

### Необязательные (по умолчанию закомментированы)

| Переменная | Когда трогать |
|---|---|
| `MDHUB_HOSTMON_MOUNTS` | если docker data root или бэкапы лежат на отдельном диске: `"/,/var/lib/docker"` |
| `MDHUB_NTP_SERVER` | если `pool.ntp.org` недоступен с VPS |
| `MDHUB_HOSTMON_*` | пороги страницы Machine (значения по умолчанию в `env.example`) |
| `MDHUB_LOG_LEVEL`, `MDHUB_TZ`, `MDHUB_ISS_RPS`, `MDHUB_HTTP_TIMEOUT`, `MDHUB_API_POOL_SIZE` | обычно не нужны |
| `MDHUB_*RETENTION*`, `MDHUB_ALGOPACK_*` | сроки хранения; значения по умолчанию подходят |
| `MDHUB_ENABLE_SDFI` | только при подписке MOEX SDFI |
| `MDHUB_UPDATE_*` | раздел самообновления (шаг 11); значения по умолчанию подходят для этой раскладки |
| `MDHUB_PG_HOST_PORT`, `MDHUB_API_HOST_PORT` | только если 5432 или 8080 на хосте заняты |
| `MDHUB_VERSION` | не нужна: версия берётся из бандла (`v0.1.0`) |

`MDHUB_UPDATE_GITHUB_TOKEN` **никогда не клади в `.env`**: он хранится в отдельном `shared/updater.env` (шаг 11).

## 5. Собрать образ и создать ключ воркера

```bash
cd /opt/mdhub/current/deploy
sudo docker compose build
sudo docker compose run --rm --no-deps worker mdhub --version          # ожидается 0.1.0
sudo docker compose run --rm --no-deps worker mdhub secrets init-key   # печатает строку MDHUB_SECRETS_PRIVATE_KEY=...
sudoedit /opt/mdhub/shared/.env                                          # вставить эту строку
```

**Сохрани копию `shared/.env` вне сервера**, например в менеджере паролей. В нём приватный ключ
воркера, без которого ключи из админки придётся вводить заново (§14).

## 6. Восстановить seed и запустить всё (§4)

```bash
cd /opt/mdhub/current/deploy
sudo ./restore_seed.sh --yes        # postgres → drop/create → pg_restore → migrate (0425…0800) → grants → up -d
sudo docker compose ps              # migrate: exited (0); остальные running/healthy
sudo docker compose --profile tls up -d caddy
sudo docker compose logs api admin hostmon | grep 'owner role' || echo "все сервисы под своими ролями"
```

## 7. Проверить выгрузку данных (§5)

```bash
cd /opt/mdhub/current/deploy
sudo docker compose run --rm --no-deps worker mdhub db-info
sudo docker compose run --rm --no-deps worker mdhub ops coverage
sudo docker compose run --rm --no-deps worker mdhub tokens create tdm-backend -s read -s export   # токен показывается один раз
curl -s https://api.example.com/health/ready
curl -s -H "Authorization: Bearer <токен>" https://api.example.com/v1/coverage | head -c 400
curl -s -H "Authorization: Bearer <токен>" "https://api.example.com/v1/fx/official?date=2026-10-16" | head -c 400
```

Дни между датой seed (16–17.10) и сегодняшним днём догрузятся сами: `quality` откроет пропуски,
`backfill_worker` будет разбирать их каждые 10 минут. Первый `quality` запускается по расписанию
в 08:30 или 20:30 МСК, раньше его можно запустить кнопкой «Run now» в админке.

## 8. Админка (§12)

```bash
cd /opt/mdhub/current/deploy
sudo docker compose run --rm --no-deps admin mdhub admin create-user <логин> --role admin --name "<Имя>"
```

- [ ] Открыть напечатанную ссылку (живёт 15 минут) с IP из allow-list, задать пароль (12–128
      символов) и зарегистрировать passkey.
- [ ] Страница **Account**: добавить TOTP-приложение и сохранить recovery-коды. Это запасной вход,
      если пропадёт passkey.
- [ ] С IP не из allow-list `https://admin.example.com` должен отвечать отказом.
- [ ] Открыть страницы Overview, Jobs, Data, Sources, API traffic, Machine, Tokens, Users, Audit.
- [ ] Остальных пользователей приглашать со страницы Users, ролями viewer, operator или admin.

Если пропал доступ: `mdhub admin reset <логин>` выдаёт ссылку сброса, `mdhub admin unlock <логин>` снимает блокировку
после 10 неудачных попыток. Обе команды запускаются через `docker compose run --rm --no-deps admin …`.

## 9. Ключи источников в админке (§14)

- [ ] Страница **Credentials**: ввести `MOEX_API_KEY`, `RATESCALC_*`, `OTC_MONITOR_TOKEN` и
      остальные. Значения никогда не показываются обратно.
- [ ] Нажать **Check** у каждого источника; результат появится в колонке «Last check».
- [ ] Если ключи всё же записаны в `.env`, их можно перенести командой
      `sudo docker compose exec worker mdhub secrets import-env`, потом очистить эти строки в
      `.env` и выполнить `sudo docker compose up -d worker api`.

## 10. Мониторинг и бэкапы (§7, §15)

- [ ] Страница **Machine** показывает CPU (steal), память, диски, сеть, часы и PostgreSQL.
      Блок контейнеров заполнится примерно через 30 секунд после старта `updater`.
- [ ] Ночной `pg_dump` в 02:00 МСК пишется в `/opt/mdhub/shared/backups`, хранятся 14 последних.
      Настрой копирование этого каталога **за пределы VPS** (rsync или restic). Бэкап на том же
      диске бэкапом не считается.
- [ ] Один раз проверь восстановление бэкапа на запасном VPS (§7).

## 11. Самообновление: пока не готово

Агент обновлений запущен, но ставить ему пока нечего: в образе нет ни одного закреплённого ключа
подписи, поэтому любой бандл он отклонит. Это безопасное поведение по умолчанию. Чтобы включить
обновления (§11, §13):

1. На Mac сгенерировать ключ minisign и закоммитить публичную часть в `deploy/release-keys/`.
2. Создать в GitHub защищённое окружение `release` с секретом и обязательным ревьюером.
3. Выпустить следующий релиз тегом `v0.1.1` или выше. Его сервер сможет поставить через Updates.
   Ключ войдёт в образ вместе с этим релизом, а `v0.1.0` на сервере ещё без ключа, поэтому
   **первое обновление до релиза с ключом делается вручную**, по §8, как раньше.
4. Опционально, чтобы обновляться прямо с GitHub: fine-grained токен с правом `contents:read`
   только на этот репозиторий.
   ```bash
   echo 'MDHUB_UPDATE_GITHUB_TOKEN=github_pat_...' | sudo tee /opt/mdhub/shared/updater.env
   sudo chmod 600 /opt/mdhub/shared/updater.env
   sudo ln -s /opt/mdhub/shared/updater.env /opt/mdhub/current/deploy/updater.env
   sudo docker compose up -d updater
   ```
5. Перед тем как полагаться на обновления в проде, прогнать репетицию на одноразовом VPS (§13).

## 12. Что ещё не готово для потребителей

- Эндпоинты `/v1/snapshots*` (snapshot-слой для TDM, SYSTEM_CONTEXT, пункт M1) в v0.1.0 **не входят**.
  Сейчас доступны `/v1/fx/*`, `/v1/futures/*`, `/v1/fixings`, `/v1/curves*`, `/v1/zcyc`, `/v1/indices/eod`,
  `/v1/calendar`, `/v1/bonds*`, `/v1/coverage`, `/v1/runs*`, `/v1/gaps` и выгрузка TDM `/v1/export/tdm`.
- Алертинга нет: состояние видно только в админке.

## 13. Если что-то пошло не так

```bash
cd /opt/mdhub/current/deploy
sudo docker compose ps
sudo docker compose logs --tail=200 migrate worker api admin hostmon updater caddy
```

Пришли мне этот вывод. В нём нет секретов: логи их маскируют. Перед отправкой всё равно проверь,
что в выводе нет паролей.
