# Особенности codex

- Hooks живут в `~/.codex/hooks.json` и понимают тот же
  `hookSpecificOutput.additionalContext`, что Claude Code (проверено `codex exec`:
  строка из вывода hook'а дошла до модели). События — `SessionStart`,
  `SessionEnd`, `SubagentStart`, `SubagentStop`, `PreToolUse`, `PostToolUse`,
  `PermissionRequest`, `PreCompact`, `PostCompact`. Репозиторий здесь ничего не
  регистрирует: hooks этого CLI заводятся вручную.
- `PreToolUse` в `codex exec` вызывается и для встроенного `apply_patch`:
  проверенный hook получил `tool_name: apply_patch`, а путь документа — в
  `tool_input.command` внутри заголовка `*** Add|Update|Delete File`.
  Для документационного hook разбирай `tool_input.file_path` у
  `Write|Edit|MultiEdit` и `tool_input.command` у `apply_patch`.
- `AGENTS.md` рабочего каталога codex читает (проверено `codex exec`:
  инструкция из файла в cwd дошла до модели). Изоляция user-слоя — оверлей
  `CODEX_HOME`: каталог симлинков на всё содержимое штатного `~/.codex`, кроме
  `AGENTS.md` и `skills`, которые указывают на нужный свод. Голая подмена
  `CODEX_HOME` на пустой каталог даёт `401 Unauthorized` — `auth.json` остаётся
  в штатном каталоге и в оверлей попадает симлинком (проверено на обеих формах).
- Своя память CLI (`~/.codex/memories`) — автосуммаризация ролаутов; на станции
  выключена ключом `[features] memories = false` в `~/.codex/config.toml`.
  Durable-факт пиши в документ репозитория правил (`AGENTS.md`): в чужой
  автосуммаризации его не увидит ни один другой исполнитель.
- Sandbox-политика проверяется без вызова модели:
  `codex sandbox -c 'sandbox_mode="workspace-write"' -- <cmd>`.
- `/bin/ps` в песочнице запрещён к исполнению (проверено: `codex sandbox --
  /bin/ps -p 1` → `execvp() failed: Operation not permitted`), поэтому каналы
  живости и уборки, построенные на `ps`, здесь не работают. Существование,
  состояние и время старта процесса читать `proc_pidinfo(PROC_PIDTBSDINFO)` из
  libproc через `ctypes` — канал работает и в песочнице (`environment.md`).
- Под `workspace-write` сессия читает пути вне рабочего дерева; результат
  задания размещай в рабочем дереве или каталоге из `writable_roots`
  (`~/.codex/config.toml`), доступность конкретного пути проверяй из этой сессии:
  иначе она отработает, а результат останется только в истории вкладки.
- Доступность записи встроенным инструментом `apply_patch` проверяй самим
  инструментом: успешная запись тем же патчем через shell или CLI-бинарник не
  подтверждает доступность tool call. В проверенных конфигурациях для пути вне
  `writable_roots` `workspace-write` отказал, а `danger-full-access` записал файл
  при `approval_policy` `never` и `on-request`.
- Пробу, воспроизводящую условия fleet-исполнителя, запускай из хостовой сессии
  с теми же параметрами песочницы: изнутри активной песочницы codex эффект её
  параметров от эффекта внешней песочницы не отделяется.
- Причину недоставленного файла ищи в транскрипте сессии
  `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-*.jsonl`: свой выбирай по
  `session_meta` первой строки — он несёт `cwd` и `session_id`. Итог лежит в
  записи `payload.type == "agent_message"` с `payload.phase == "final_answer"`,
  прочие такие записи имеют `phase == "commentary"`; финальной нет — сессия до
  итога не дошла. Прочитанное — диагностика: гейт доставки оно не закрывает и
  файл результата не заменяет.
- Прогон проектного тулчейна под `workspace-write` требует кэш каждого
  инструмента прогона в `writable_roots` (`~/.codex/config.toml`): каталоги
  спрашивай у самих инструментов (`go env GOCACHE`, `GOLANGCI_LINT_CACHE`,
  `PRE_COMMIT_HOME` и их дефолты), они лежат в `$HOME` и под запрет записи
  попадают целиком. Отказ приходит не запретом записи, а результатом самой
  проверки — `operation not permitted` в выводе линтера, `no go files to
  analyze`, `PermissionError` в хуке (SB-199), — поэтому красный прогон тулчейна
  в codex-сессии сверяй с прогоном вне песочницы, прежде чем читать его как
  дефект кода.
- Пробу `codex sandbox` запускать из хостовой сессии: внутри уже активной
  песочницы Codex она падает раньше своей команды (`sandbox_apply: Operation not
  permitted`), и отказ читается как запрет проверяемой команды.
- Внутри пробы нет пригодного tmp (`No usable temporary directory`), а `python3`
  разрешается в 3.9 из CommandLineTools без `tomllib`: интерпретатор передавать
  абсолютным путём (`which python3` хостовой сессии), тесты с `tempfile` через
  эту пробу не гонять.
- Проверку, которой нужен реальный OSA с приложением, codex-сессии не поручать —
  гонять её запускающему (`make test-osa`): в песочнице `tell application` падает
  `(-1728)`, а файл-скрипт с таким блоком — на компиляции `(-2741)`, что читается
  как дефект скрипта или формы аргументов. Сам `osascript` работает (проверено:
  `-e 'return 1 + 1'` → `2`), поэтому его доступность признаком не является.
