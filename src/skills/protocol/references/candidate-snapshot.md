# Снимок кандидата

Перед этапами Implementation и Acceptance прочитай этот справочник вместе с `git-lifecycle.md`.

## Идентификаторы

- `IMPLEMENTATION_START` — неизменный коммит начала Implementation.
- `BASE` — последний candidate, переданный независимому checker; после его запуска принимает возвращённый `next_base`.
- `CANONICAL_REF` — полное имя канонической ветви задачи, например `refs/heads/feature/ABC-123`.
- `CANONICAL_BASE` — коммит `CANONICAL_REF`, включённый в текущий candidate.
- `CANDIDATE` — проверяемый `HEAD` ветви исполнителя.

`BASE` и `CANONICAL_BASE` имеют разные назначения и не заменяют друг друга.

## Подготовка кандидата

1. После изменений выполни запланированные локальные проверки и создай проверенный промежуточный candidate commit. Незакоммиченный или непроверенный результат не передавай в base-sync.
2. Обнови канонический ref и синхронизируй candidate по разделу «Обновление базы кандидата» в `git-lifecycle.md`. Сдвиг базы аннулирует прежние проверки этого candidate; после него выполняется полный контур Implementation и новый Acceptance.
3. Запусти mutating project prepare, если его задаёт профиль. Прими в рабочее дерево только допустимые изменения и классифицируй их до commit.
4. Повтори все запланированные проверки после base-sync и mutating-команд. Закоммить созданные изменения; итоговый `HEAD` становится `CANDIDATE`.
5. На точном `CANDIDATE` запусти project verify в read-only режиме. Команда должна проверять candidate в disposable окружении или гарантировать отсутствие записи в target. Сохрани команду, cwd, время, код, SHA candidate и лог.
6. Создай новый manifest; запечатанный файл не перезаписывай:

   ```bash
   CANDIDATE="$(git -C "$WORKTREE_ROOT" rev-parse HEAD)"
   MANIFEST="$TASK_DIR/candidate-manifest-$CANDIDATE.json"
   python3 "$PROTOCOL_SKILL/scripts/candidate_manifest.py" seal \
     --repository "$WORKTREE_ROOT" \
     --implementation-start "$IMPLEMENTATION_START" \
     --canonical-ref "$CANONICAL_REF" \
     --canonical-base "$CANONICAL_BASE" \
     --check-base "$BASE" \
     --candidate "$CANDIDATE" \
     --artifact plan "$TASK_DIR/plan.md" \
     --artifact reconciliation "$TASK_DIR/implementation-reconciliation.md" \
     --artifact final-checks-summary "$FINAL_CHECKS_SUMMARY" \
     --output "$MANIFEST"
   ```

   Добавь отдельный `--artifact <role> <absolute-path>` для каждого handoff, реестра findings и обязательного evidence вне Git. Не включай mutable `task.md`, `progress`, незавершённый `result` и lock-файлы. Если конкретного project check нет, не передавай `final-checks-summary`.

`seal` требует чистый worktree, точный `HEAD == CANDIDATE`, существующие коммиты, `CANONICAL_REF`, разрешающийся ровно в `CANONICAL_BASE`, ancestry всех баз и уникальные обычные файлы. Manifest получает `image_id` как SHA-256 canonical JSON его содержимого без самого `image_id`.

## Проверка снимка

Перед checker, в начале и в конце Acceptance выполни:

```bash
python3 "$PROTOCOL_SKILL/scripts/candidate_manifest.py" verify "$MANIFEST"
```

- Код `0` подтверждает тот же candidate, canonical base и внешние артефакты.
- Код `2` означает повреждённый или неполный manifest и блокирует этап.
- Код `3` означает `stale`: verdict не формируется. На Acceptance выполни `return-to-implementation`, обнови базу кандидата и повтори полный контур Implementation.

Передавай checker и Acceptance путь manifest, его SHA-256 и `image_id`. Результат каждого независимого прохода должен вернуть тот же `image_id`.
