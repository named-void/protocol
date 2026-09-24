---
name: test-protocol
description: 'Триггер: точное сообщение `test-protocol UPL-CODE`; двухфазная автоматизированная проверка изменённых пар «метод × роль» UPL на Develop с обязательным вторым прогоном расширенной фазы.'
---

# Проверка прав доступа UPL на стенде

Триггер — точное сообщение `test-protocol UPL-<code>`; команда не открывает цикл доработки и не разрешает изменения кода, Jira, Git, Wiki и `state.md`.

1. Разреши `PROTO` от физического расположения skill (симлинк каталога навыка checkout не даёт): `PROTO="$(dirname "$(realpath '<путь загруженного test-protocol/SKILL.md>')")/../.."` — каталог `src` репозитория протокола. Проверь `test -r` для `$PROTO/skills/protocol/scripts/resolve_issue.py` и `$PROTO/lib/skills_config.py`, отсутствие — блокер. Запусти `python3 "$PROTO/skills/protocol/scripts/resolve_issue.py" <UPL-code>`, сохрани `KEY` и `project_roots`.
   `DATA_ROOT` получи только с контекстом проекта — `cd <project_roots[0]> && python3 "$PROTO/lib/skills_config.py" data-root` либо `AGENTS_PROJECT=<проект> python3 "$PROTO/lib/skills_config.py" data-root`; аргументы команда игнорирует, а без контекста молча возвращает `projects/_common/.data`, где state задачи нет.
2. Прочитай единственный `<DATA_ROOT>/tasks/<KEY>/state.md` (`TASK_DIR`) и `$PROTO/../projects/upl/environment.md`. Если `state.md` отсутствует, перепроверь контекст `DATA_ROOT` из шага 1; блокируй только после повторной проверки. Продолжай только при `status: completed`, заполненных `candidate` и `checked_candidate`, принятом результате и обязательных evidence, иначе — блокер без изменений.
   Ячейки — пары «метод × роль», где задача изменила и метод, и доступ роли; ожидаемый HTTP-статус бери из требований задачи с их координатой (требования приоритетнее общей документации, конфликт источников — блокер). Неоднозначные роли или статус — блокер.
   Обнови кэш матрицы прав: `python3 "$PROTO/../projects/upl/scripts/build_perm_matrix.py" --migrations "<корень UPL из projects/upl/project.toml>/services/auth-service/migrations" --out "<DATA_ROOT>/upl/perm-matrix.json"` — пересборка только при миграциях новее сохранённых, неразобранный стейтмент — блокер. Матрица «domain × action → роли» с `path_pattern` и `is_public` — источник фактических составов ролей домена для manifest.
3. Подготовь manifest на каждый метод в `<TASK_DIR>/test-protocol/methods/`; одна ячейка — один сценарий:

```json
{
  "required": [
    {"id": "method-role", "role": "<role>", "method": "GET", "path": "/api/v1/<resource>", "expected_status": [200]}
  ],
  "extended": []
}
```

Для `PUT`/`PATCH` добавь `prepare` с `GET` и `save_as`, `json_from` с минимальным `json_patch` и cleanup с восстановлением; для `DELETE` — `prepare` с `POST` уникального fixture, capture его id; target (path или query) и cleanup обязаны ссылаться на этот capture (`{имя.пути}` либо `$capture`).
Cleanup-`DELETE` обязан ссылаться на идентификатор записи, созданной POST-prepare этого сценария (в пути или query — `{имя.пути}` либо `$capture`); удаления по role/status/датам, захардкоженному id или capture из GET-lookup запрещены, restore-`PUT`/`PATCH` cleanup этого ограничения не имеет. Если ячейке нужна существующая запись под fixture, помечай её lookup-`GET` в `prepare` флагом `"prerequisite": true`.
Цель назначения системного поля (например `is_contact = true`) бери с уже установленным значением: cleanup раннера восстанавливает только захваченный объект, а перенос признака с прежнего держателя невосстановим; цель — текущий держатель делает сценарий самовосстанавливающимся.
4. Покажи план и запусти: `python3 "$PROTO/../projects/upl/scripts/test_protocol_methods.py" --manifest <manifest> --phase required --plan-only`, затем то же без `--plan-only`.
   Пользователей ролей и cookie-сессии на `UPL_DEV_BASE_URL` разрешает `test_protocol_auth.py` (активный пользователь роли из service DB: `users.role_code`, `deleted_at IS NULL`); при заданном `UPL_DEV_BOOTSTRAP_USER_ID` роли резолвятся через API без БД — один dev-login этого пользователя и `GET /api/v1/users?role_codes=<role>&is_deleted=false` с пагинацией.
   Нет пользователя роли — ячейки `SKIP`, прогон продолжается; при нескольких активных пользователях роли раннер берёт первого по `id` — для сессии этого достаточно.
5. Выдай результат по каждой ячейке: ожидалось, получено, `PASS/FAIL/SKIP/BLOCKED`, статусы prepare/cleanup. Критерий — только HTTP-статус целевого вызова; cookie, токены и `user_id` не выводи. `BLOCKED` — prepare-шаг с `"prerequisite": true` получил 404: подходящая под fixture запись отсутствует, ячейка не проверена до появления записи; любой другой сбой prepare — `FAIL`. stdout раннера содержит `user_id` в путях шагов — перед выводом в чат и лог маскируй UUID.
6. Extended — обязательный второй прогон: при required без `FAIL` и `BLOCKED` запусти остальные роли этих же методов (`--phase extended`); ожидаемый статус роли бери из требований задачи, при их отсутствии — из матрицы прав: `is_public` ячейки «domain × action» или роль в её составе — 200, иначе — 403.
   Цель extended-ячейки обновления профиля бери чужой относительно роли актора (роль цели ≠ роль актора): self-update игнорирует проверяемое поле (например `is_contact`) и даёт ложный 200 вместо ожидаемого запрета. Ожидания prepare/cleanup у запрещённых extended-ячеек бери `[200, 403]`: отказ в чтении цели или cleanup-сброс после несостоявшейся мутации не сбой — критерий только статус целевого вызова. Любой `FAIL` или `BLOCKED` в required — extended не запускай и зафиксируй блокер.
   Незапущенные extended-ячейки в итог `required` не включай.

Границы: Dev БД — только read-only `SELECT`; изменяющие запросы — только сценарии manifest с уникальным fixture или восстановлением и обязательным cleanup; неописуемый сценарий — блокировка ячейки до запросов.

Auth-диагностика: при одновременном `FAIL` основной проверки ячейки и противоречии полученного статуса с постановкой задачи разрешён read-only `SELECT` permissions конкретных ролей из БД auth на том же postgres-сервере; подтверждённое принципиальное разрешение или запрет контура для сбойной пары укажи в результате как наиболее вероятную причину.
