# market-data-hub

Market data hub for the trading desk backend: loaders (MOEX ISS/APIM, CBR, ECB, Ratescalcs,
Cbonds), update rules and a PostgreSQL 16 store with full history and raw captures.

- Architecture and data model: [`docs/DESIGN.md`](docs/DESIGN.md)
- Extending the code base, conventions, tests: [`docs/DEVELOPING.md`](docs/DEVELOPING.md)
- Role in the Trading Desk Monitor system, the snapshot contract it owns and the system work list: [`docs/SYSTEM_CONTEXT.md`](docs/SYSTEM_CONTEXT.md)

```bash
uv sync --python 3.12
export MDHUB_DATABASE_URL=postgresql://mdhub@localhost/mdhub
uv run mdhub migrate          # apply schema migrations
uv run mdhub jobs list        # registered jobs and their schedule
uv run mdhub run <job> -p key=value
uv run mdhub scheduler        # run the schedule (Europe/Moscow)
uv run pytest -q              # tests (throw-away PostgreSQL via pgserver)
```
