# Жизненный цикл Git

Перед выполнением этого справочника прочитай `$PROTOCOL_SKILL/SKILL.md`, раздел
`Bootstrap`; он устанавливает `PROTOCOL_SKILL`, `PROTOCOL_SRC` и обязательные
справочники среды.

## Branch-Sync

1. Прочитай навык `git-workflow` и засей конфигурные переменные:

   ```bash
   eval "$(python3 "$PROTOCOL_SRC/lib/skills_config.py" export-env)"
   ```

2. Для issue выбери тип ветви из `vcs.branch_types`. Явный тип из поручения приоритетен;
   иначе `Bug` даёт `bugfix`, срочное исправление продуктивного сбоя — `hotfix`, остальное —
   `feature`. Для редакции сохрани тип канонической ветви её базы. Если этот тип не разрешён картой, остановись на конфликте требований.
3. Для редакции передай каноническую ветвь исходной задачи или предыдущей редакции как базу:

   ```bash
   AGENTS_BASE_BRANCH="<type>/<base-task>" \
     "$AGENT_SKILLS_DIR/git-workflow/scripts/sync-branch.sh" <KEY-N> <type>
   ```

   Для `common-N` используй тип `protocol` и текущую ветвь как базу:

   ```bash
   AGENTS_BASE_BRANCH="<исходная-ветвь>" \
     "$AGENT_SKILLS_DIR/git-workflow/scripts/sync-branch.sh" common-N protocol
   ```

   Для обычного issue вызови тот же скрипт как `sync-branch.sh <KEY> <type>`. Если `origin` не настроен,
   добавь `AGENTS_LOCAL_ONLY=1`; настроенный `origin` не обходи этим флагом.
4. Ненулевой код блокирует старт. Значение кода читай в
   `$AGENT_SKILLS_DIR/git-workflow/references/scripts.md`; не обходи отказ
   ручным созданием ветви или worktree.
5. Из строки `created:`, `switched:` или `current:` получи каноническую ветвь, фактический тип, ветвь исполнителя и путь worktree. Для `created:` база указана в строке; для `switched:` и `current:` возьми её из существующего `task.md`. Если записанной базы нет, остановись: выбор базы по догадке меняет состав diff. Сверь `git status --short` целевого worktree и продолжай работу только в нём. Сохрани полное имя `CANONICAL_REF=refs/heads/<type>/<task>` и текущий полный SHA этой ветви как `CANONICAL_BASE`; checker-переменную `BASE` ими не заменяй.

## Обновление базы кандидата

В конце Implementation после проверенного промежуточного candidate commit, но перед mutating project prepare и созданием финального candidate, обнови канонический ref:

1. Убедись, что tracked-состояние worktree проверено, закоммичено и чисто. Повтори Branch-Sync той же командой и с теми же параметрами: она выполнит fetch, обновит локальный канонический ref и не сольёт его в ветвь исполнителя, если у неё уже есть собственные коммиты.
2. Получи `CURRENT_CANONICAL_BASE` командой `git rev-parse "$CANONICAL_REF^{commit}"`. Если он равен сохранённому `CANONICAL_BASE`, дополнительный sync не нужен.
3. Если SHA изменился, в `WORKTREE_ROOT` выполни `git merge --no-edit "$CANONICAL_REF"`. Конфликт блокирует продолжение до явного разрешения. После успешного merge установи `CANONICAL_BASE=$CURRENT_CANONICAL_BASE`.
4. Сдвиг базы аннулирует прежний manifest, Implementation checker и Acceptance. Повтори mutating prepare, все запланированные проверки, read-only verify, sealing и полный Implementation checker; fast path в первой версии нет.

В начале Acceptance повтори только Branch-Sync и проверку manifest. Если канонический ref сдвинулся, не выполняй merge внутри Acceptance: состояние `stale` возвращает задачу на этап 5, где применяется порядок выше.

## Локальная приёмка

1. Убедись, что `HEAD` ветви исполнителя совпадает с последним принятым `CANDIDATE` этапа 5, а `git status --short` worktree пуст. Затем сверь каноническую ветвь с актуальным `origin`, если он настроен. Несовпадение снимка или грязный worktree возвращает Acceptance в Implementation как `stale` и блокирует вызов `accept-worktree.sh`.
2. Сформируй однострочный заголовок по `vcs.commit_title_format`; он должен начинаться с `<task> `.
3. Выполни:

   ```bash
   EXPECTED_CANONICAL_BASE="$CANONICAL_BASE" \
     "$AGENT_SKILLS_DIR/git-workflow/scripts/accept-worktree.sh" \
       <task> <type> "<заголовок>"
   ```

   Перед вызовом снова засей конфигурные переменные; для `common-N` передай тип `protocol`, для редакции — её полный идентификатор и сохранённый тип ветви.
4. Код `0` означает, что ветвь исполнителя влита в каноническую, а worktree и ветвь исполнителя удалены. Код `27` означает, что каноническая база сдвинулась после Acceptance: интеграция не начата, верни задачу на этап 5 как `stale`. Код `25` оставляет worktree для разрешения конфликта; код `26` означает, что merge уже выполнен, но уборка не завершена. Остальные ненулевые коды — блокеры; команду вслепую не повторяй.
5. Зафиксируй в `task.md` команду, итоговый sha канонической ветви и состав коммитов. Push и создание MR/PR
   остаются за человеком.
