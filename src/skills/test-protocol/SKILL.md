---
name: test-protocol
description: 'Триггер: точное сообщение `test-protocol UPL-CODE`; двухфазная автоматизированная проверка изменённых пар «метод × роль» UPL на Develop с последующим опциональным расширением.'
---

# Проверка завершённой UPL-задачи

## Триггер

Точное сообщение `test-protocol UPL-<code>` (после удаления внешних пробелов) запускает skill самостоятельно; предварительный `read-protocol` не требуется. Команда не входит в постановку задачи, не открывает цикл доработки и не разрешает изменения Jira, Git, Wiki, кода или `state.md`; локальные manifests сценариев можно готовить в `<TASK_DIR>/test-protocol/`. Упоминание в прозе, issue key без команды, URL вместо ключа и тематическое совпадение не активируют skill.

## Bootstrap

Перед первым действием выполни одним shell-вызовом:

```bash
set -eu

TEST_PROTOCOL_SKILL_FILE='<абсолютный путь загруженного test-protocol/SKILL.md>'
TEST_PROTOCOL_AGENT='<имя текущего CLI, не имя оболочки>'
TEST_PROTOCOL_SKILL="$(dirname "$(realpath "$TEST_PROTOCOL_SKILL_FILE")")"
TEST_PROTOCOL_SRC="$(cd "$TEST_PROTOCOL_SKILL/../.." && pwd -P)"
TEST_PROTOCOL_INSTRUCTIONS="$TEST_PROTOCOL_SRC/instructions"
test -r "$TEST_PROTOCOL_INSTRUCTIONS/environment.md"
test -r "$TEST_PROTOCOL_SRC/lib/skills_config.py"
test -r "$TEST_PROTOCOL_SRC/skills/protocol/scripts/resolve_issue.py"
test -r "$TEST_PROTOCOL_SRC/../projects/upl/scripts/test_protocol_auth.py"
test -r "$TEST_PROTOCOL_SRC/../projects/upl/scripts/test_protocol_methods.py"
printf 'TEST_PROTOCOL_SKILL=%s\nTEST_PROTOCOL_SRC=%s\n' "$TEST_PROTOCOL_SKILL" "$TEST_PROTOCOL_SRC"
cat "$TEST_PROTOCOL_INSTRUCTIONS/environment.md"

TEST_PROTOCOL_AGENT_INSTRUCTIONS="$TEST_PROTOCOL_INSTRUCTIONS/agent-$TEST_PROTOCOL_AGENT.md"
test ! -e "$TEST_PROTOCOL_AGENT_INSTRUCTIONS" || cat "$TEST_PROTOCOL_AGENT_INSTRUCTIONS"
```

Сохрани абсолютные `TEST_PROTOCOL_SKILL` и `TEST_PROTOCOL_SRC` как константы сессии; отсутствующий справочник исполнителя не блокирует маршрут. Перед использованием project-specific окружения прочитай `<UPL_ROOT>/environment.md`.

## Локальные входы

1. Передай `UPL-<code>` из сообщения в `python3 "$TEST_PROTOCOL_SRC/skills/protocol/scripts/resolve_issue.py"`; сохрани нормализованный `KEY` и `project_roots`. Resolver — только локальная проверка ключа и карты проекта; Jira issue и auth-service не читай.
2. Получи `DATA_ROOT` командой `python3 "$TEST_PROTOCOL_SRC/lib/skills_config.py" data-root`.
3. Найди ровно один `<DATA_ROOT>/tasks/<KEY>/state.md` и продолжай только при `status: completed`, заполненных `candidate` и `checked_candidate`, принятом результате и обязательных evidence.
4. Прочитай из `state.md` задачу, scope, требования, критерии приёмки и источники; далее — только нужные перечисленные в нём локальные snapshots, `inputs`, `plan.md` или notes. История итераций — не источник требований.
5. Для контрактов из общей документации используй локальный snapshot, при его отсутствии — только указанную проектной картой или state страницу через read-only `adapter-wiki` (предварительно прочитай его `SKILL.md` из `AGENT_SKILLS_DIR`). `adapter-issues` и `adapter-vcs` не вызывай.

При отсутствии state, неоднозначном кандидате, status не `completed`, непроверенном candidate или недостаточных evidence покажи конкретный блокер и остановись: без Branch-Sync, worktree, нового цикла, артефактов и любых изменений.

## Две фазы покрытия

Фаза `required` — только пары «метод × роль», где задача изменила и метод, и доступ этой роли к нему; не расширяй её до всех ролей метода из общей документации. Если изменённые роли определить однозначно нельзя — блокер вместо догадки.

Фаза `extended` — те же методы и остальные роли, явно связанные с ними в задаче или общей документации; запускается только после успешного прохождения всех ячеек `required` и отдельного согласия пользователя.

Требования задачи приоритетны для метода и ожидаемого HTTP-статуса; общая документация дополняет список ролей и отсутствующий контракт. Конфликт источников, неизвестный ожидаемый статус, отсутствие метода или роли в источниках блокируют фазу.

До первого запроса к стенду выдай схему выбранной фазы — по пункту на пару «метод × роль»: `[METHOD] /path`, роль, источник и координату требования, ожидаемый HTTP-статус, сценарий подготовки данных и способ очистки. Контракты и ожидаемые статусы фиксированы до первого запроса; в фазе тестирования выполняется только вызов изменённых методов на dev-стенде под ролями ячеек и сверка HTTP-кодов с ожидаемыми, без чтения кода, миграций и диффов.

## Автоматизация

Исполняемые скрипты: `<UPL_ROOT>/scripts/test_protocol_auth.py` и `<UPL_ROOT>/scripts/test_protocol_methods.py`. На каждый метод — отдельный manifest в `<TASK_DIR>/test-protocol/methods/`; один сценарий manifest — одна ячейка «метод × роль». Не объединяй разные методы в универсальный ручной shell-вызов.

Минимальная форма manifest:

```json
{
  "required": [
    {
      "id": "method-role",
      "role": "<role>",
      "method": "GET",
      "path": "/api/v1/<resource>",
      "expected_status": [200]
    }
  ],
  "extended": []
}
```

Для `PUT`/`PATCH` добавь `prepare` с `GET` и `save_as`, `json_from` с минимальным `json_patch` поверх сохранённого исходного тела и cleanup с восстановлением исходных значений. Для `DELETE` добавь `prepare` с контрактным `POST` создания уникального fixture, capture его идентификатора (удаляй именно созданный объект) и cleanup. Произвольные записи и массовые операции не используй.

Сначала покажи план без запросов, затем запусти выбранную фазу тем же manifest без `--plan-only`; `--phase extended` — только после отдельного согласия пользователя:

```bash
python3 <UPL_ROOT>/scripts/test_protocol_methods.py --manifest <manifest> --phase required --plan-only
```

Изменяющие запросы разрешены только в объявленном manifest-сценарии с уникальным fixture или восстановлением исходного тела и обязательным cleanup; статус cleanup учитывай в результате. Если такой сценарий описать нельзя — заблокируй ячейку до запросов.

`test_protocol_auth.py` read-only выбирает пользователя каждой роли через `psql` к UPL service DB (`users.role_code`, `deleted_at IS NULL`; при нескольких подходящих — первый по id), используя `UPL_DEV_DATABASE_URL` или PG* environment; auth-service, permission-таблицы и сведения о ролях из auth не читает. Каждая роль — отдельная cookie-сессия в памяти. Прямой доступ к Dev БД — только read-only `SELECT`: подбор пользователя роли и идентификаторов/атрибутов сущностей ячеек (`brands`, `vendors`, `publishers`, `users_partners`) для fixture и целевых запросов; `INSERT`, `UPDATE`, `DELETE` и DDL напрямую в БД запрещены.

Все вызовы идут через общий домен `UPL_DEV_BASE_URL` (по умолчанию `https://develop.getblogger.ru`); конкретный сервис определяется только path manifest. Вход в сессию — `POST /auth/dev/login` с `{"user_id":"<user_id из service DB>","auth_type":"cookie"}`; поле `role` не передавай. `XSRF-TOKEN` для изменяющего запроса добавляется автоматически, ручной `X-USER-ROLE` не добавляется.

## Авторизация и критерий результата

До первого вызова разреши `user_id` из service DB для всех ролей выбранной фазы. Отсутствие активного пользователя роли — не блокер: пометь её ячейки `SKIP` и продолжи прогон остальных ролей; отсутствие доступа к БД или входа блокирует запуск неполной фазы. Не используй ID из примера, локальной БД другого стенда или Jira.

Критерий ячейки — совпадение HTTP-статуса ответа целевого метода с ожидаемым для роли; успешный login сам по себе доступ не подтверждает. Тело ответа, бизнес-логику и побочные поля не проверяй, если это не отдельный критерий задачи; для диагностики выводи только размер и SHA-256 тела. Не маскируй ошибку авторизации ошибкой payload: подготовительный и cleanup-запросы имеют свои ожидаемые статусы.

После выполнения выдай по одному результату на каждую ячейку выбранной фазы: `ожидалось`, `получено`, `PASS/FAIL` или `SKIP`, method/path/role, статусы подготовки и cleanup, безопасную координату запроса и доказательство. Cookie, токены, `user_id` и секреты не выводи и не сохраняй в `state.md`, evidence, логах или комментариях QA.

Если все ячейки `required` выполнены с ожидаемым HTTP-статусом и успешным cleanup либо помечены `SKIP`, покажи итог обязательной фазы и отдельно спроси пользователя о запуске `extended` для остальных ролей этих методов; при отказе итог — `required passed; extended not run`, при согласии — отдельный результат расширенной фазы. При любом FAIL обязательной ячейки вопрос о расширении не задавай.
