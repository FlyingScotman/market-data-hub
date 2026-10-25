# Обновление до 0.2.0 (WEB-15: история для монитора, контракт 1.2)

Для владельца. Состояние 21.10.2026: production = **0.1.3**, `main` после PR этой ветки
содержит всё, что накопилось с 0.1.3 (HR2, HR3, HS1–HS13, контракт 1.1), плюс WEB-15.
Агент не ставил тегов, не публиковал релиз и не трогал production: всё ниже делает владелец.

## Что приходит

- **Read-API истории** `/v1/history/{index,bonds,curves/{dataset},zcyc,fixings,fx}`, scope
  `read`, колоночный JSON, курсоры, `ETag`/`Last-Modified` (304 без чтения строк), gzip.
  Справочник: [`docs/HISTORY_API.md`](../docs/HISTORY_API.md).
- **Контракт `market_snapshot/1.2`** (аддитивно, монитор читает любой 1.x): в
  `bond_quotes[].source_analytics` поля `duration_waprice_days`, `yield_last_coupon_pct`,
  `icpi`, `ir`, `bei`, `cbr`; в `fixings[]` — `hub_ingested_at`; RUSFARIND теперь `unit = index`,
  неизвестные серии `RUSFAR*` уходят в `excluded` с причиной `rusfar_series_unit_unknown`.
- **Пропуски кривых видны:** наборы `curves_ruonia_ois`, `curves_key_rate_swap` (`business_daily`,
  с 05.01.2026) открывают `ops.data_gap` и видны в `/v1/gaps` и `/v1/coverage`.
- **Готовность входов `eod_final`:** срез 08:00 ждёт до 30 минут финальные `bonds_eod`/`zcyc`
  за D, затем публикуется со `stale_reasons`, запуск `partial`.
- **Автокоррекция:** бэкфилл, изменивший входы даты с уже опубликованным `eod_final`, ставит
  пересборку (задание `snapshot_rebuild`); новый срез с `supersedes` выходит только при другом
  `content_sha256`.
- **Календари до 2037** (`curated_unverified`, отмечаются `provisional_from`), правило TARGET до 2037.

Не входит (нужен владелец, VPS или лицензии): разбор 502 Ratescalcs на production, канал
алертов, источник рейтингов, кривые USD/EUR OIS, загрузчики SOFR и €STR (хосты NY Fed и ECB Data
Portal недоступны из среды разработки, записанных ответов нет, доступ с VPS не проверен — WEB-4).

## Версия

Версия берётся из git-тега (hatch-vcs). Предполагаемый тег после слияния PR в `main`:
**`v0.2.0`** (новый API, контракт 1.2, миграция). Если до слияния владелец выпустит `main` как
`v0.1.7`, этот PR всё равно выходит как `v0.2.0`. `min_previous_version` не менялся (`0.1.0`),
промежуточный релиз не нужен.

## Миграции

Одна новая миграция этого PR:

- `0427_history_api` — `CREATE INDEX IF NOT EXISTS curve_snapshot_history_idx ON
  md.curve_snapshot (dataset, snapshot_date, source, fetched_at DESC, id DESC)`. Только индекс,
  данные не меняются. Строится в транзакции миграции (не `CONCURRENTLY`): на время построения
  (секунды, таблица — десятки тысяч строк) запись в `md.curve_snapshot` ждёт; воркер на шаге
  миграции всё равно остановлен (drain).

С 0.1.3 придут и миграции `main`, ещё не установленные на production: `0110_issuer_reference`,
`0426_coupon_basis_amortizer`, `0520_issuer_rating_known_at`, `0611_issuers_job_source`.
Точный список покажет страница подтверждения в Updates. Все аддитивны; откат через миграции —
только восстановлением pre-update дампа (его делает updater).

Новые настройки (необязательны, значения по умолчанию рабочие; в `.env` ничего добавлять не
нужно): `MDHUB_SNAPSHOT_EOD_FINAL_WAIT_MINUTES=30` (сколько ждать входы `eod_final`, 0 — не
ждать), `MDHUB_SNAPSHOT_AUTOCORRECT=true` (автокоррекция срезов после бэкфилла).

## Выпуск и установка (штатный механизм Updates)

1. Слить PR в `main`, проверить зелёный CI на коммите слияния.
2. Создать и отправить тег (процедура README_DEPLOY §11, «The protected environment
   `release`»):

   ```bash
   git switch main && git pull --ff-only     # локальный main = коммит слияния
   git tag -a v0.2.0 -m "market-data-hub 0.2.0"
   git push origin v0.2.0
   ```

   - `git tag -a` — аннотированный тег; `-m` — его сообщение. hatch-vcs возьмёт версию 0.2.0
     из этого тега; release.yml проверит, что тег равен версии пакета.
   - `git push origin v0.2.0` запускает `.github/workflows/release.yml`: ci → build → sign
     (ждёт одобрения в окружении `release`) → verify → publish.
3. В Actions одобрить шаг `sign`. Дождаться GitHub Release с тремя файлами (`.tar.gz`,
   `.SHA256SUMS`, `.SHA256SUMS.minisig`).
4. В консоли <https://hub.example.com> → **Updates** → релиз 0.2.0 → «Review and install».
   Updater проверит подпись и манифест, сделает дамп, соберёт образ, остановит воркер (drain),
   применит миграции, перезапустит worker/api/admin и проверит `/health/ready`, heartbeat и
   `/v1/coverage`; при ошибке откатится сам.
5. **Только для перехода с 0.1.3** (WEB-10): updater 0.1.3 не перезапускает `hostmon`.
   После `done` пересоздать его вручную с тем же override (под root):

   ```bash
   dc() { docker compose -f /opt/mdhub/current/deploy/docker-compose.yml \
                         -f /opt/mdhub/shared/compose.local.yaml "$@"; }
   dc up -d --no-deps hostmon
   dc ps hostmon
   ```

   - `dc` — короткая функция: `-f` дважды = основной compose-файл текущего релиза плюс
     постоянный override (host nginx, loopback-порты, путь к секретам).
   - `up -d` — создать/пересоздать сервис в фоне; `--no-deps` — не трогать зависимые сервисы
     (PostgreSQL и остальные уже работают).
   - `ps hostmon` — убедиться, что контейнер `running`; версию видно на странице Machine.

## После установки

Все команды — на VPS под root, с функцией `dc` из шага 5. Токены и `.env` не печатать.

1. **Токен для монитора** (история требует scope `read`; `read` открывает и `/v1/snapshots*`,
   так что одного токена хватает на оба пути):

   ```bash
   dc run --rm --no-deps worker mdhub tokens create tdm-history -s read --expires-days 365
   ```

   - `run --rm` — разовый контейнер воркера, удаляется после команды; `--no-deps` — не
     запускать зависимости.
   - `tokens create tdm-history` — именованный токен `tdm-history`; `-s read` — scope;
     `--expires-days 365` — срок действия год.
   - Токен печатается **один раз** в stdout: сразу положить его в хранилище секретов монитора
     (не в чат, не в репозиторий). Отозвать: `mdhub tokens revoke tdm-history`.

2. **Календари 2028–2037** запишутся сами при ежемесячном обновлении (первый рабочий день
   месяца, 09:10). Чтобы сразу:

   ```bash
   dc run --rm --no-deps worker mdhub run cbr_meetings -p mode=calendar
   ```

   - `mdhub run <job>` — выполнить задание один раз (запись в `ops.job_run`, trigger `cli`);
     `-p mode=calendar` — режим ежемесячного запуска: обновить календари, затем заседания ЦБ.

3. **Пропуски кривых** посчитает плановый `quality` в 20:30 или 08:30. Сразу:

   ```bash
   dc run --rm --no-deps worker mdhub run quality \
       -p datasets=curves_ruonia_ois,curves_key_rate_swap,snapshot_eod_final \
       -p check_jobs=false -p catch_up=false
   ```

   - `-p datasets=...` — проверить только новые наборы; `-p check_jobs=false` — без здоровья
     заданий; `-p catch_up=false` — не перезапускать пропущенные плановые запуски.
   - Ожидание: у `curves_*` откроются пропуски на рабочих днях 2026 года без единого набора
     (WEB-11/WEB-13 насчитали 8 таких дат); `GET /v1/gaps?dataset=curves_ruonia_ois` их покажет.
     Бэкфилла у них нет: закрываются только появлением снимка за дату.

4. **Проверка API** (с Mac или VPS; токен в переменной окружения, не в команде):

   ```bash
   H="Authorization: Bearer $MDHUB_TOKEN"; B=https://hub.example.com
   curl -sS "$B/health/ready"
   curl -sS -H "$H" "$B/v1/history/index" | head -c 800; echo
   curl -sS -H "$H" --compressed -D - -o /dev/null \
        "$B/v1/history/bonds?from=2026-10-01&secid=SU26238RMFS4,SU52005RMFS4&fields=close_pct,icpi"
   curl -sS -H "$H" "$B/v1/history/fixings?from=2026-10-01&code=RUSFAR,RUSFARIND" | head -c 800
   curl -sS -H "$H" "$B/v1/gaps?dataset=curves_ruonia_ois"
   ```

   - `-sS` — без прогресса, но с текстом ошибки; `-H "$H"` — заголовок с токеном;
     `--compressed` — запросить gzip и распаковать; `-D -` — вывести заголовки ответа;
     `-o /dev/null` — тело не печатать; `head -c 800` — первые 800 байт ответа.
   - Ожидание: `/health/ready` — `"version":"0.2.0"`, `pending` 0; в заголовках `/history/bonds`
     — `ETag: W/"..."`, `Last-Modified`, `Content-Encoding: gzip`; повтор с
     `-H 'If-None-Match: W/"<etag>"'` даёт `HTTP/1.1 304`; у `RUSFARIND` `"index"`, у `RUSFAR`
     `"pct"`.

5. **Необязательно — пересборка срезов 2026 года как 1.2.** Новые поля уже есть в
   `/v1/history/*`, поэтому монитору это не обязательно. Если владелец хочет, чтобы и
   финальные срезы 05.01–20.10.2026 несли поля 1.2 (каждый выйдет корректирующим срезом с
   `supersedes`, ~1 мин на дату, всего несколько часов, нагрузка на БД умеренная):

   ```bash
   dc run -d --rm --no-deps worker mdhub run snapshot_rebuild \
       -p date_from=2026-01-05 -p date_to=2026-10-20
   ```

   - `run -d` — в фоне (команда не привязана к SSH-сессии); прогресс: `/v1/runs?job=snapshot_rebuild`.
   - `snapshot_rebuild` пересобирает **только уже опубликованные** `eod_final` диапазона,
     каждый со своим `cut_ts`, как известно сейчас; новых дат не создаёт, `known_at` не подменяет.
   - Это решение владельца (как HR5 в WEB-14): решить до запуска.

## Откат

Штатно: Updates → «Roll back to 0.1.3» восстановит pre-update дамп (миграции были). Данные,
загруженные после дампа, вернёт бэкфилл. Ручной порядок — README_DEPLOY §13 «Manual recovery».
