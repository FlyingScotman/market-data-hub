# Обновление до 0.3.2 (WEB-18: рейтинги, сектора, условия выпуска, контракт 1.3)

> **Историческая запись.** Документ писался как «UPGRADE_TO_0.4.0»; эта работа вышла тегом **v0.3.2**
> (22.10.2026) и установлена на production вместе с 0.3.3 / 0.3.4 (FlyingScotman/market-data-hub#20).
> Номер 0.4.0 занял следующий релиз — контракт `market_snapshot/1.5`, см. [UPGRADE_TO_0.4.0.md](UPGRADE_TO_0.4.0.md).
> Ниже текст 21.10 без изменений: версия «0.4.0» в нём означает 0.3.2.

Для владельца. Состояние 21.10.2026: production = **0.2.0**; `main` уже содержит 0.3.0 (WEB-16,
PR #6: ставки, кроссы, ИПЦ, рыночная часть контракта 1.3) и 0.3.1 (WEB-17, PR #5: исправления EOD). Этот PR (ветка
`claude/hub-static-ratings-terms`) добавляет рейтинги агентств, сектор по данным агентств,
механизм условий выпуска с evidence, признак замещающих облигаций и контракт
`market_snapshot/1.3`. Агент не ставил тегов, не публиковал релиз и не трогал production:
всё ниже делает владелец.

Этот PR слит с `main` (7d6daef) и **расширяет** контракт `market_snapshot/1.3` из WEB-16 (static
часть), версию 1.4 не вводит. Установка 0.4.0 поверх 0.2.0 приносит и 0.3.0/0.3.1: сначала
выполнить шаги «После установки» из [UPGRADE_TO_0.3.0.md](UPGRADE_TO_0.3.0.md) и
[UPGRADE_TO_0.3.1.md](UPGRADE_TO_0.3.1.md), затем шаги ниже (они независимы).

## Что приходит

- **Рейтинги из публичного раскрытия агентств** (задание `ratings_agencies`, ежедневно 06:50):
  НКР (две Excel-выгрузки), НРА (выгрузка действий с 2020 года), Эксперт РА (страницы списков,
  карта сайта → пресс-релизы → страницы компаний, регионов и выпусков; бюджет запросов на
  запуск). Рейтинги выпусков сопоставляются с MOEX по ISIN (`ref.bond_rating`), рейтинги
  эмитентов — по ИНН (`ref.issuer_rating`), регионы Эксперт РА — через ISIN их выпусков. История
  действий — `ref.rating_action`. АКРА **не** загружается: сайт отдаёт неполную цепочку TLS
  (нет промежуточного сертификата GlobalSign GCC R46 OV TLS CA 2025), проверенного соединения нет.
- **Сектор по отрасли агентства** (`sector_source = agency`): заполняет только пробелы
  (эмитенты без сектора по curated / группе / ОКВЭД / классу выпусков / названию); расхождение
  агентств — сектор не назначается; списки «Торговля» не отображаются (дистрибьюторы любых отраслей).
- **Условия выпуска с evidence** (задание `bond_terms_curated`, ежедневно 06:55): файл
  `src/mdhub/data/bond_terms_curated.json` — конвенции флоатеров (лаг, усреднение, floor/cap),
  call бессрочных, датированные оферты, признак замещающей облигации; на каждую запись — ссылка
  на документ, страница и цитата пункта. Файл **пустой**: документы (e-disclosure, НРД) из
  среды разработки недоступны, без цитаты значения не вносятся.
- **Контракт `market_snapshot/1.3`** (аддитивно, монитор читает любой 1.x): `ratings[]` с
  `scope = issuer`, `grade`, `rating_type`, `press_release`, `mapping_basis`; в `instruments[]`
  `replacement_bond` + `replacement_basis` (`document` / `name_rule`), `issue_size_moex` +
  `issue_size_moex_field` (число бумаг по MOEX, **не** объём в обращении);
  `evidence_document_url` у формул и оферт из документов; `sector_source = agency`.
- **API:** `/v1/bonds/{secid}` дополнительно отдаёт `issuer_ratings`; в `ratings` новые поля.
- **Мониторинг:** пробы источников `nkr`, `nra`, `raexpert` (robots.txt, раз в час) на странице
  Sources; наборы свежести `ratings_nkr`, `ratings_nra`, `ratings_raexpert` в `/v1/coverage`
  (устаревают, если последнему действию агентства больше 10 дней).

Не входит: ОКВЭД (ЕГРЮЛ / pb.nalog.ru / bo.nalog.ru из среды разработки не отвечают — обрыв
соединения; с VPS в России не проверено), АКРА (TLS), автоматическое чтение документов
e-disclosure (анти-бот 403) и НРД (ЦКИ — платный сервис).

## Права на данные

Все три агентства в условиях сайта запрещают воспроизведение и распространение без согласия.
Хаб использует данные для внутреннего деска (как и Cbonds); срезы и API не передавать
третьим лицам. Решение об использовании — за владельцем (ревью §7.4).

## Версия

Предполагаемый тег после слияния PR в `main`: **`v0.4.0`** (новые задания и таблицы, новые поля
контракта 1.3; после 0.3.1). Тег не ставился. `min_previous_version` не менялся (`0.1.0`).

## Миграции

Пять новых, все аддитивны:

- `0120_credit_ratings` — источники `nkr`, `nra`, `raexpert`, `acra` в `ops.source`; таблицы
  `ref.agency_rating`, `ref.rating_action`, `ref.agency_page`, `ref.issuer_rating`; новые
  колонки `ref.bond_rating` (`watch`, `action`, `grade`, `rating_type`, `evidence`); значение
  `agency` в CHECK `ref.issuer.sector_source`; история строк.
- `0121_bond_curated_terms` — `ref.bond_curated_term` (+ история).
- `0540_rating_known_at`, `0541_curated_terms_known_at` — функции «как известно на момент»
  для срезов.
- `0612_ratings_job_source` — связь задания `ratings_agencies` с источниками (страница Sources).

Новых настроек нет.

## Выпуск и установка (штатный механизм Updates)

1. Слить PR в `main`, проверить зелёный CI на коммите слияния.
2. Тег и релиз (README_DEPLOY §11):

   ```bash
   git switch main && git pull --ff-only
   git tag -a v0.4.0 -m "market-data-hub 0.4.0"
   git push origin v0.4.0
   ```

   - `git switch main` — перейти на `main`; `git pull --ff-only` — подтянуть коммит слияния,
     только перемоткой (без локального merge).
   - `git tag -a v0.4.0` — аннотированный тег, из него hatch-vcs возьмёт версию 0.4.0;
     `-m` — сообщение тега.
   - `git push origin v0.4.0` — отправить тег; это запускает `release.yml` (ci → build → sign
     с одобрением в окружении `release` → verify → publish).
3. В Actions одобрить `sign`, дождаться GitHub Release с тремя файлами.
4. Консоль <https://hub.example.com> → **Updates** → 0.4.0 → «Review and install». Updater
   проверит подпись, сделает дамп, остановит воркер, применит миграции, перезапустит сервисы и
   проверит `/health/ready`; при ошибке откатится сам.

## Первичная загрузка (после установки)

Команды — на VPS под root. Функция `dc` — как в UPGRADE_TO_0.2.0 (шаг 5):

```bash
dc() { docker compose -f /opt/mdhub/current/deploy/docker-compose.yml \
                      -f /opt/mdhub/shared/compose.local.yaml "$@"; }
```

- `-f` дважды — основной compose-файл текущего релиза плюс постоянный override (host nginx,
  loopback-порты, путь к секретам).

1. **Доступ к агентствам с VPS.** Страница Sources: пробы `nkr`, `nra`, `raexpert` должны быть
   `ok`. Плановый `source_probe` проверяет их раз в час; сразу:

   ```bash
   dc run --rm --no-deps worker mdhub run source_probe -p sources=nkr,nra,raexpert -p force=true
   ```

   - `run --rm` — разовый контейнер воркера, удаляется после команды; `--no-deps` — не
     запускать зависимые сервисы (PostgreSQL уже работает).
   - `mdhub run source_probe` — одно выполнение задания проб; `-p sources=…` — только эти
     источники; `-p force=true` — не ждать часового интервала проб.

2. **НКР и НРА** (4 запроса, меньше минуты):

   ```bash
   dc run --rm --no-deps worker mdhub run ratings_agencies -p agencies=nkr,nra
   ```

   - `-p agencies=nkr,nra` — только эти агентства (список через запятую); Эксперт РА — шаг 3.
   - Ожидание: статус `succeeded`; в результате (`/v1/runs?job=ratings_agencies`) — `nkr`
     ~430 объектов, `nra` ~210, `mapping`, `coverage.active_non_structured`.

3. **Эксперт РА, первичный обход** (около 3 050 страниц: 1 750 пресс-релизов за 400 дней и
   ~1 270 страниц, на которые они ссылаются; 0,5 запроса/с — **около 1 ч 45 мин**, замер 21.10):

   ```bash
   dc run -d --rm --no-deps worker mdhub run ratings_agencies \
       -p agencies=raexpert -p max_requests=6000
   ```

   - `run -d` — в фоне, не привязано к SSH-сессии; прогресс — `/v1/runs?job=ratings_agencies`
     (поле `raexpert.backlog`) или страница Jobs.
   - `-p agencies=raexpert` — только Эксперт РА; `-p max_requests=6000` — бюджет страниц
     на этот запуск (по умолчанию 400: плановый ежедневный запуск дочитывает остаток понемногу).
   - Если запуск прервался или бюджета не хватило — повторить ту же команду: обход
     инкрементальный (`ref.agency_page`), прочитанное не перечитывается.
   - Ежедневный плановый запуск 06:50 затем читает 12 списков, карту сайта и только новые
     пресс-релизы (несколько десятков запросов).

4. **Сектора по отрасли агентств** (плановый `issuers_static` в 07:00 сделает сам; сразу):

   ```bash
   dc run --rm --no-deps worker mdhub run issuers_static
   ```

   - Задание пересчитывает `sector` / `sector_source` всех эмитентов в периметре; результат
     содержит `sector_assigned` / `sector_unassigned`.

5. **Static-срез 1.3** выйдет плановым запуском 07:45. Сразу (корректирующий срез того же дня,
   если содержимое изменилось):

   ```bash
   dc run --rm --no-deps worker mdhub run snapshot_static -p mode=daily
   ```

   - `-p mode=daily` — ежедневный срез (cut 07:45 текущего дня); другой режим — `change`.

6. **Проверка через API** (токен в переменной окружения, не в команде):

   ```bash
   H="Authorization: Bearer $MDHUB_TOKEN"; B=https://hub.example.com
   curl -sS "$B/health/ready"
   curl -sS -H "$H" "$B/v1/bonds/RU000A10F9F8" | python3 -m json.tool | grep -A12 '"ratings"'
   curl -sS -H "$H" "$B/v1/coverage" | python3 -m json.tool | grep -A6 '"ratings_'
   curl -sS -H "$H" "$B/v1/snapshots?kind=static&latest=true" | head -c 600; echo
   ```

   - `-sS` — без прогресса, с текстом ошибки; `-H "$H"` — заголовок с токеном;
     `python3 -m json.tool` — форматировать JSON; `grep -A12` — строка и 12 следующих.
   - Ожидание: `"version":"0.4.0"`; у RU000A10F9F8 (ИКС 5 ФИНАНС 003P-23) рейтинг НКР
     `AAA.ru`, `grade` `AAA`; наборы `ratings_nkr` / `ratings_nra` / `ratings_raexpert`
     не `stale`; в манифесте последнего static `schema_version` `market_snapshot/1.3` и
     coverage датасета `ratings` с `active_non_structured_bonds_rated`.

## Курирование условий выпуска (когда будут документы)

Запись в `src/mdhub/data/bond_terms_curated.json` (формат — в поле `description` файла):
`secid`, необязательный `isin`, `term` (`floater_formula` / `call` / `offer` /
`replacement_bond`), `value`, `evidence` (`document_url`, `document_title`, `page`,
`extracted_text` — пункт дословно, `retrieved_on`, `curated_by`). Изменение файла — обычный
PR и релиз; задание `bond_terms_curated` применит его, а убранные записи пометит удалёнными.
Приоритет (ревью P1-9): флоатеры на КС/RUONIA из портфеля деска, затем бессрочные без даты
call — их числа в coverage `floater_formulas` среза (`key_rate_ruonia_floaters` против
`..._curated`, `active_perpetuals` против `..._with_scheduled_call`).

## Откат

Штатно: Updates → «Roll back» к установленной до этого версии восстановит pre-update дамп. Данные агентств,
загруженные после дампа, вернёт следующий запуск `ratings_agencies`.
