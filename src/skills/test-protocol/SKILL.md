---
name: test-protocol
description: 'Триггер: точное сообщение `test-protocol UPL-CODE`; двухфазная автоматизированная проверка изменённых пар «метод × роль» UPL на Develop с последующим опциональным расширением.'
---

# Проверка прав доступа UPL на стенде

Триггер — точное сообщение `test-protocol UPL-<code>`; команда не открывает цикл доработки и не разрешает изменения кода, Jira, Git, Wiki и `state.md`.

1. Разреши `PROTO` от физического расположения skill (симлинк каталога навыка checkout не даёт): `PROTO="$(dirname "$(realpath '<путь загруженного test-protocol/SKILL.md>')")/../.."` — каталог `src` репозитория протокола. Проверь `test -r` для `$PROTO/skills/protocol/scripts/resolve_issue.py` и `$PROTO/lib/skills_config.py`, отсутствие — блокер. Запусти resolver с кодом задачи, сохрани `KEY` и `project_roots` (`UPL_ROOT`); `DATA_ROOT` получи командой `python3 "$PROTO/lib/skills_config.py" data-root`.
2. Прочитай единственный `<DATA_ROOT>/tasks/<KEY>/state.md` (`TASK_DIR`) и `<UPL_ROOT>/environment.md`. Продолжай только при `status: completed`, заполненных `candidate` и `checked_candidate`, принятом результате и обязательных evidence, иначе — блокер без изменений. Ячейки — пары «метод × роль», где задача изменила и метод, и доступ роли; ожидаемый HTTP-статус бери из требований задачи с их координатой (требования приоритетнее общей документации, конфликт источников — блокер). Неоднозначные роли или статус — блокер.
3. Подготовь manifest на каждый метод в `<TASK_DIR>/test-protocol/methods/`; одна ячейка — один сценарий:

```json
{
  "required": [
    {"id": "method-role", "role": "<role>", "method": "GET", "path": "/api/v1/<resource>", "expected_status": [200]}
  ],
  "extended": []
}
```

Для `PUT`/`PATCH` добавь `prepare` с `GET` и `save_as`, `json_from` с минимальным `json_patch` и cleanup с восстановлением; для `DELETE` — `prepare` с `POST` уникального fixture, capture его id и cleanup.
4. Покажи план и запусти: `python3 <UPL_ROOT>/scripts/test_protocol_methods.py --manifest <manifest> --phase required --plan-only`, затем то же без `--plan-only`. Пользователей ролей и cookie-сессии на `UPL_DEV_BASE_URL` разрешает `test_protocol_auth.py` (активный пользователь роли из service DB: `users.role_code`, `deleted_at IS NULL`); нет пользователя роли — ячейки `SKIP`, прогон продолжается.
5. Выдай результат по каждой ячейке: ожидалось, получено, `PASS/FAIL/SKIP`, статусы prepare/cleanup. Критерий — только HTTP-статус целевого вызова; cookie, токены и `user_id` не выводи.
6. Все `required`-ячейки `PASS` или `SKIP` — спроси согласие на `extended` (остальные роли этих же методов; отдельный запуск с `--phase extended`, незапущенные extended-ячейки в итог `required` не включай); при отказе итог `required passed; extended not run`. При любом `FAIL` вопрос не задавай.

Границы: Dev БД — только read-only `SELECT`; изменяющие запросы — только сценарии manifest с уникальным fixture или восстановлением и обязательным cleanup; неописуемый сценарий — блокировка ячейки до запросов.

Auth-диагностика: при одновременном `FAIL` основной проверки ячейки и противоречии полученного статуса с постановкой задачи разрешён read-only `SELECT` permissions конкретных ролей из БД auth на том же postgres-сервере; подтверждённое принципиальное разрешение или запрет контура для сбойной пары укажи в результате как наиболее вероятную причину.
