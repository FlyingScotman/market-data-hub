#!/bin/bash
# SessionStart-хук Claude Code: ставит CLI CodeGraph в облачной сессии.
# Правило CodeGraph в AGENTS.md требует `codegraph` в каждом checkout, а облачный
# контейнер создаётся без него. Локально хук ничего не делает. Индекс не строит:
# init/sync выполняет агент по AGENTS.md. Файл одинаковый в четырёх репозиториях
# программы (FlyingScotman/trading-desk-monitor#411).
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

CODEGRAPH_PACKAGE="@colbymchenry/codegraph@1.6.2"

if npm ls --global --depth=0 "$CODEGRAPH_PACKAGE" >/dev/null 2>&1; then
  exit 0
fi

# Бинарник приходит optional-зависимостью из официального реестра npm.
# Вывод npm уходит в stderr, чтобы не попасть в контекст сессии.
npm install --global "$CODEGRAPH_PACKAGE" \
  --registry=https://registry.npmjs.org --no-audit --no-fund >&2
