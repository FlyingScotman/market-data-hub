# Обновление до 0.3.0 (WEB-16: €STR, SOFR, кроссы, прокси-кривые, ИПЦ г/г; контракт 1.3)

Для владельца. Состояние 21.10.2026: production = **0.2.0**. Агент не ставил тегов, не
публиковал релиз и не трогал production: всё ниже делает владелец. Предполагаемый тег после
слияния PR: **`v0.3.0`** (новые задания, таблица, контракт 1.3). `min_previous_version` не менялся.

## Что приходит

| Набор (`/v1/coverage`) | Что | Источники по порядку (fallback) | История |
|---|---|---|---|
| `estr` | `md.fixing` `ESTR`, % годовых, по дням TARGET | ECB Data Portal → Bundesbank → DBnomics → FRED | с 2019-10-01 |
| `sofr`, `sofr_averages` | `SOFR`, `SOFR30DAVG`, `SOFR90DAVG`, `SOFR180DAVG` (%), `SOFRINDEX` (`unit = index`) | NY Fed → FRED | SOFR с 2018-04-02, средние с 2020-03-02 |
| `ecb_fx` (было) | курсы ЕЦБ | www.ecb.europa.eu → **ECB Data Portal** (те же значения) | с 2015 |
| `fx_cross_cbr_cross` | кроссы из двух официальных курсов ЦБ на дату действия | данные хаба (`md.fx_official`) | с 2015-01-01 |
| `fx_cross_moex_indicative_cross` | кроссы из индикативных курсов MOEX 19:00 | данные хаба (`md.fx_indicative`) | с 2015-03-30 |
| `fx_cross_moex_cets` | торгуемые кроссы CETS (`EURUSD000TOM`, `USDCNY_TOM`, …), только дни со сделками | данные хаба (`md.fx_eod`); сделок нет с 12.06.2024 | история |
| `fx_cross_fred_h10` | H.10 ФРС (`DEXUSEU`, `DEXCHUS`, …) | FRED | с 2015 |
| `proxy_usd_ust_par` | кривая par-доходностей US Treasury, **прокси, не SOFR OIS** | home.treasury.gov | с 2024-01-02 |
| `proxy_eur_aaa_par` | par-доходности AAA euro area ЕЦБ, **прокси** | ECB Data Portal → (другая кривая) Bundesbank `eur_bund_par` | с 2024-01-02 |
| `cbr_cpi_yoy` | `CPI_RU_YOY` — официальный ИПЦ г/г (%), как его публикует ЦБ | www.cbr.ru `/hd_base/infl/` | с 2013-09 |
| `rosstat_cpi` (было) | + `published_on` новейшего месяца (дата документа на странице Росстата) | rosstat.gov.ru | с 1991 |

- Ряды разных источников — **отдельные серии** (`source`), не склеиваются; правило выбора для
  потребителя: в каждый день первый присутствующий источник в порядке таблицы.
- **Контракт `market_snapshot/1.3`** (аддитивно): `cross_rates[].kind` += `cbr_cross`,
  `moex_indicative_cross`, `moex_cets`, `fred_h10`; `base_ccy` любой; поля `source`, `labels`.
  Новые фиксинги и прокси-кривые идут в существующие `fixings` / `curve_quote_sets` без изменения
  схемы; прокси-кривые помечены `labels: ["mid_only", "proxy_ust_par"]` (и `proxy_ecb_aaa_par`,
  `proxy_bund_par`).
- **History API:** `/v1/history/fixings?code=ESTR,SOFR,CPI_RU_YOY`, `/v1/history/curves/usd_ust_par`,
  `/v1/history/fx?kind=ecb,cbr_cross,moex_indicative_cross,moex_cets,fred_h10&pair=EUR/USD`,
  `/v1/history/index` перечисляет все новые серии.
- **`mdhub probe-sources`** — проверка с сервера, какие источники отвечают (ниже).
- Новая настройка `MDHUB_SKIP_SOURCES` (необязательна): коды источников, которые цепочки не
  пробуют (если с VPS хост заблокирован — экономит таймауты).

## Что отвечало из среды разработки (21.10.2026)

Это **не** VPS; с VPS проверить командой из шага 1 после установки.

| Источник | Хост | Отсюда |
|---|---|---|
| `ecb_data_portal` | data-api.ecb.europa.eu | отвечает (EST, EXR, YC) |
| `ecb` | www.ecb.europa.eu | отвечает (с VPS — нет, WEB-4) |
| `nyfed` | markets.newyorkfed.org | отвечает (SOFR, SOFRAI) |
| `us_treasury` | home.treasury.gov | отвечает (XML за месяц ~0,5–20 с) |
| `cbr`, `moex_iss`, `rosstat` | www.cbr.ru, iss.moex.com, rosstat.gov.ru | отвечают |
| `bundesbank` | api.statistiken.bundesbank.de | заблокирован политикой среды (не проверено) |
| `dbnomics` | api.db.nomics.world | заблокирован политикой среды (не проверено) |
| `fred` | fred.stlouisfed.org | заблокирован политикой среды (не проверено) |
| `banque_de_france` | webstat.banque-france.fr | заблокирован политикой среды; документированный API требует ключ — только проба, загрузчика нет |

Для трёх непроверенных источников парсеры написаны по документированному формату; тестовые
фикстуры повторяют формат с реальными значениями ЕЦБ / NY Fed (`tests/fixtures/rates/README.md`).
**Проба с сервера обязательна** перед тем, как на них полагаться.

## Миграции

- `0260_md_fx_cross_rates_sources` — источники `ecb_data_portal`, `bundesbank`, `dbnomics`,
  `fred`, `nyfed`, `us_treasury`; таблица `md.fx_cross` (+ история строк, права `mdhub_api`).
- `0530_fx_cross_known_at` — читатель `md.fx_cross_known_at` для срезов и `known_at`.
- `0660_job_source_rates_fx_cpi` — связи заданий с источниками (`ops.v_source_status`).

Все аддитивны, данные существующих таблиц не меняются. Номера выбраны вне диапазонов
параллельной работы; на базе 0.2.0 применяются как ожидающие.

## Выпуск и установка

Как для 0.2.0 ([UPGRADE_TO_0.2.0.md](UPGRADE_TO_0.2.0.md), шаги 1–4): слить PR, зелёный CI,
тег `v0.3.0`, одобрить `sign`, установить через Updates.

## После установки

Все команды — на VPS под root. Функция `dc`:

```bash
dc() { docker compose -f /opt/mdhub/current/deploy/docker-compose.yml \
                      -f /opt/mdhub/shared/compose.local.yaml "$@"; }
```

- `docker compose -f A -f B` — основной compose-файл текущего релиза плюс постоянный override;
  `"$@"` — передать дальше все аргументы функции.

1. **Какие источники отвечают с сервера:**

   ```bash
   dc run --rm --no-deps worker mdhub probe-sources
   dc run --rm --no-deps worker mdhub probe-sources --json > /tmp/probe-sources.jsonl
   ```

   - `run --rm` — разовый контейнер воркера, удаляется после команды; `--no-deps` — не
     запускать зависимые сервисы (базе проба не нужна, она ничего не пишет).
   - `worker` — сервис, в образе которого есть `mdhub` и сеть/прокси как у загрузчиков
     (`MDHUB_HTTP_PROXY` из `.env`).
   - `mdhub probe-sources` — по одному реальному запросу на (источник, набор), ответ разбирается
     тем же парсером, что у загрузчика; таблица: `source`, `dataset`, `role` (primary /
     fallback N / input / probe only), `ok`, `http`, `ms`, `bytes`, `result` (последняя дата и
     значение или ошибка).
   - `--json` — по объекту JSON на строку (для истории/сравнения); `> файл` — сохранить вывод.
   - Дополнительно: `-s <код>` (повторяемо) — только этот источник, напр. `-s fred -s bundesbank`;
     `--timeout 30` — таймаут запроса в секундах (по умолчанию 20).
   - Код выхода 0 — все **primary** ответили, 1 — нет (fallback-и только отображаются).
   - Прислать вывод разработчику (секретов в нём нет: все источники публичные).

2. **Если хосты заблокированы** — убрать их из цепочек, чтобы каждый запуск не ждал таймаут:

   ```bash
   sudoedit /opt/mdhub/shared/.env     # добавить, например: MDHUB_SKIP_SOURCES=bundesbank,fred
   dc up -d --no-deps worker
   ```

   - `sudoedit` — безопасное редактирование файла от root; `.env` общий для всех релизов.
   - `MDHUB_SKIP_SOURCES` — список кодов через запятую (`ecb_data_portal`, `bundesbank`,
     `dbnomics`, `fred`, `ecb`); пустое значение — пробовать все.
   - `up -d --no-deps worker` — пересоздать только воркер с новым окружением, в фоне.

3. **Первичная загрузка истории.** Плановые запуски сами засеют пустые ряды с начала истории
   (первый запуск каждого задания); чтобы сразу, по очереди:

   ```bash
   dc run --rm --no-deps worker mdhub run estr_fixing
   dc run --rm --no-deps worker mdhub run sofr_fixing
   dc run --rm --no-deps worker mdhub run cbr_inflation
   dc run --rm --no-deps worker mdhub run rosstat_cpi -p date_from=2026-09-01
   dc run --rm --no-deps worker mdhub run proxy_curves
   dc run --rm --no-deps worker mdhub run fx_cross
   ```

   - `mdhub run <задание>` — выполнить один раз (запись в `ops.job_run`, trigger `cli`); без
     `-p date_from/date_to` пустой ряд грузится с начала истории (€STR 2019-10-01, SOFR
     2018-04-02, ИПЦ г/г 2013-09, прокси-кривые 2024-01-02, кроссы 2015-01-01), иначе — последние
     10 дней. Результат JSON показывает `source` (кто отдал данные) и `tried` (все попытки).
   - `rosstat_cpi -p date_from=2026-09-01` — перечитать книгу Росстата за месяцы с сентября 2026,
     чтобы проставить `published_on` новейшему месяцу (у старых месяцев дат выпуска нет — их не
     выдумываем).
   - `proxy_curves` — без параметров обе кривые (`-p curves=usd` или `-p curves=eur` — одну);
     US Treasury за 2024–2026 — три годовых XML, по ~20 с каждый.
   - `fx_cross` идёт **после** `cbr_daily`/`fx_indicative` (читает их строки); `-p sources=cbr,moex_iss`
     — без FRED, если он заблокирован; `-p date_from=2015-01-01 -p date_to=2026-10-21` — явный
     диапазон.
   - Если www.ecb.europa.eu с VPS не отвечает, ECB-курсы с 15.10 догрузит запасной хост:
     `dc run --rm --no-deps worker mdhub run ecb_fx -p date_from=2026-10-15` (`-p date_from` —
     первая дата диапазона; конец — сегодня).
   - Явный источник: `-p sources=bundesbank` (только он) или `-p sources=fred,ecb_data_portal`
     (этот порядок вместо стандартного).

4. **Проверка пропусков по новым наборам** (иначе — плановый `quality` в 08:30 / 20:30):

   ```bash
   dc run --rm --no-deps worker mdhub run quality \
       -p datasets=estr,sofr,sofr_averages,cbr_cpi_yoy,proxy_usd_ust_par,proxy_eur_aaa_par,fx_cross_cbr_cross,fx_cross_moex_indicative_cross \
       -p check_jobs=false -p catch_up=false
   ```

   - `-p datasets=...` — только эти наборы; `-p check_jobs=false` — без проверки здоровья
     заданий; `-p catch_up=false` — не перезапускать пропущенные плановые запуски.
   - Ожидание: праздники США у `sofr` / `proxy_usd_ust_par` (календарь пн–пт) закрываются как
     `unfillable`, когда источник подтвердил отсутствие данных; остальное — 0 открытых пропусков.

5. **Проверка API** (токен в переменной окружения, не в команде):

   ```bash
   H="Authorization: Bearer $MDHUB_TOKEN"; B=https://hub.example.com
   curl -sS -H "$H" "$B/v1/history/fixings?from=2026-10-01&code=ESTR,SOFR,CPI_RU_YOY" | head -c 800; echo
   curl -sS -H "$H" "$B/v1/history/fx?from=2026-10-15&kind=ecb,cbr_cross&pair=EUR/USD" | head -c 800; echo
   curl -sS -H "$H" "$B/v1/history/curves/usd_ust_par?from=2026-10-15" | head -c 800; echo
   ```

   - `-sS` — без прогресса, но с текстом ошибки; `-H "$H"` — заголовок с токеном;
     `head -c 800` — первые 800 байт ответа; `echo` — перевод строки после обрезанного вывода.
   - Ожидание: `ESTR` с `source` `ecb_data_portal` (или запасного источника), `cbr_cross`
     EUR/USD на каждый день, у `usd_ust_par` в `labels` есть `proxy_ust_par`.

## Откат

Штатно: Updates → «Roll back to 0.2.0» восстановит pre-update дамп (миграции были). Таблица
`md.fx_cross` и новые фиксинги при этом исчезнут; повторная установка загрузит их заново.
