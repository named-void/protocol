#!/usr/bin/env bash
# Подготовка и финальная проверка сервиса на базе шаблона templ-service.
# Живёт в carrier `protocol/projects/upl`; запускать из корня сервисного repo.
# Сервис-специфика берется из окружения: SERVICE_ROOT (git toplevel), configs/.env и .env,
# DB_CONTAINER для нестандартного имени контейнера БД.
#
# Usage:
#   final-checks.sh prepare
#   CANDIDATE_REF=<full-sha> final-checks.sh verify
#
# prepare запускает mutating project checks в task-worktree.
# verify проверяет точный candidate в disposable worktree и не меняет target.
set -u

MODE="${1:-}"
if [[ $# -ne 1 || ( "$MODE" != "prepare" && "$MODE" != "verify" ) ]]; then
  printf 'usage: %s prepare | CANDIDATE_REF=<full-sha> %s verify\n' "$0" "$0" >&2
  exit 64
fi

ORIGINAL_SERVICE_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
# Сам скрипт лежит в git-репозитории проектного слоя, поэтому git-toplevel сервис
# не опознаёт: запуск из каталога скрипта дал бы проверку чужого дерева с
# правдоподобными сигналами. Признак сервиса — go.mod в корне.
if [[ ! -f "$ORIGINAL_SERVICE_ROOT/go.mod" ]]; then
  printf 'блокер: %s не сервисный репозиторий (go.mod в корне не найден) — запускай из корня сервиса\n' "$ORIGINAL_SERVICE_ROOT" >&2
  exit 99
fi

VERIFY_PARENT=""
VERIFY_ROOT=""
cleanup_verify_worktree() {
  local exit_code=$? cleanup_code=0
  trap - EXIT
  if [[ -n "$VERIFY_ROOT" ]]; then
    git -C "$ORIGINAL_SERVICE_ROOT" worktree remove --force "$VERIFY_ROOT" >/dev/null 2>&1 \
      || cleanup_code=99
  fi
  if [[ -n "$VERIFY_PARENT" && -d "$VERIFY_PARENT" ]]; then
    rmdir "$VERIFY_PARENT" >/dev/null 2>&1 || cleanup_code=99
  fi
  if [[ "$exit_code" -eq 0 && "$cleanup_code" -ne 0 ]]; then
    exit_code="$cleanup_code"
  fi
  exit "$exit_code"
}
trap cleanup_verify_worktree EXIT

CANDIDATE="unsealed"
SERVICE_ROOT="$ORIGINAL_SERVICE_ROOT"
VERIFY_SEED_PATH_LIST=()
if [[ "$MODE" == "verify" ]]; then
  if [[ ! "${CANDIDATE_REF:-}" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]]; then
    printf 'блокер: verify требует CANDIDATE_REF с полным Git SHA\n' >&2
    exit 99
  fi
  CANDIDATE="$(git -C "$ORIGINAL_SERVICE_ROOT" rev-parse --verify "${CANDIDATE_REF}^{commit}" 2>/dev/null)" \
    || { printf 'блокер: CANDIDATE_REF не разрешается в commit: %s\n' "$CANDIDATE_REF" >&2; exit 99; }
  if [[ "$CANDIDATE" != "$CANDIDATE_REF" ]]; then
    printf 'блокер: CANDIDATE_REF должен быть canonical full SHA: %s != %s\n' "$CANDIDATE_REF" "$CANDIDATE" >&2
    exit 99
  fi
  if [[ "$(git -C "$ORIGINAL_SERVICE_ROOT" rev-parse HEAD)" != "$CANDIDATE" ]]; then
    printf 'блокер: HEAD target не совпадает с CANDIDATE_REF\n' >&2
    exit 99
  fi
  if [[ -n "$(git -C "$ORIGINAL_SERVICE_ROOT" status --porcelain)" ]]; then
    printf 'блокер: verify требует чистый target worktree\n' >&2
    exit 99
  fi

  VERIFY_PARENT="$(mktemp -d "${TMPDIR:-/tmp}/upl-final-checks-verify.XXXXXX")" \
    || { printf 'блокер: не удалось создать disposable каталог verify\n' >&2; exit 99; }
  VERIFY_ROOT="$VERIFY_PARENT/worktree"
  git -C "$ORIGINAL_SERVICE_ROOT" worktree add --quiet --detach "$VERIFY_ROOT" "$CANDIDATE" \
    || { printf 'блокер: не удалось создать disposable worktree для %s\n' "$CANDIDATE" >&2; exit 99; }
  SERVICE_ROOT="$VERIFY_ROOT"

  # Некоторые UPL-сервисы собираются с gitignored generated inputs. Копируем
  # только явно перечисленные относительные пути и после прогона сравниваем их
  # с target, чтобы mutating verify не прошёл незамеченным.
  read -r -a VERIFY_SEED_PATH_LIST <<< "${FINAL_CHECKS_VERIFY_SEED_PATHS:-api/docs}"
  for relative_path in "${VERIFY_SEED_PATH_LIST[@]}"; do
    if [[ "$relative_path" = /* || "$relative_path" == ".." || "$relative_path" == ../* \
      || "$relative_path" == */.. || "$relative_path" == */../* ]]; then
      printf 'блокер: небезопасный FINAL_CHECKS_VERIFY_SEED_PATHS: %s\n' "$relative_path" >&2
      exit 99
    fi
    if [[ -e "$ORIGINAL_SERVICE_ROOT/$relative_path" ]]; then
      # `cp -R src dst` при существующем dst кладёт src ВНУТРЬ dst
      # (api/docs/docs/): в обоих наблюдавшихся сервисах в api/docs лежат
      # tracked-файлы, поэтому каталог в worktree уже существует, и вложенная
      # копия роняла typecheck или purity. Копируем содержимое с `/.`,
      # досоздавая каталог при его отсутствии.
      mkdir -p "$VERIFY_ROOT/$relative_path"
      cp -R "$ORIGINAL_SERVICE_ROOT/$relative_path/." "$VERIFY_ROOT/$relative_path/" \
        || { printf 'блокер: не удалось скопировать verify input %s\n' "$relative_path" >&2; exit 99; }
    fi
  done
fi

# Имя сервиса — из основного рабочего дерева репозитория, а не из каталога
# worktree: в worktree задачи basename даёт имя каталога (probe-736), и кандидат
# ${SERVICE_NAME}-db никогда не совпадает с контейнером стенда (SB-118).
SERVICE_MAIN_ROOT="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || printf '%s/.git' "$SERVICE_ROOT")")"
SERVICE_NAME="$(basename "$SERVICE_MAIN_ROOT")"
LOG_DIR="${TMPDIR:-/tmp}/${SERVICE_NAME}-final-checks-${MODE}-$(date +%Y%m%d%H%M%S)-$$"
SUMMARY_FILE="$LOG_DIR/summary.txt"
CHANGED_FILE="$LOG_DIR/changed-files.txt"
STATUS_FILE="$LOG_DIR/git-status.txt"

mkdir -p "$LOG_DIR"
cd "$SERVICE_ROOT" || exit 99
export PATH="$(go env GOPATH)/bin:$PATH"

# Кэш golangci-lint хранится в per-run temp вне worktree: кэш внутри worktree создавал untracked-файлы (SB-68), а общий кэш сохранял пути удалённых worktree и падал с `no such file` (SB-49). Цена — холодный кэш на каждый прогон.
export GOLANGCI_LINT_CACHE="$LOG_DIR/golangci-lint"
mkdir -p "$GOLANGCI_LINT_CACHE"

failed=0

status() {
  printf '%s: %s\n' "$1" "$2" | tee -a "$SUMMARY_FILE"
}

status "mode" "$MODE"
status "candidate" "$CANDIDATE"

summarize_failure() {
  local log_file="$1"
  local lines

  lines="$(grep -E '(^--- FAIL:|FAIL|[Ee]rror|undefined|cannot|vet:|level=error)' "$log_file" | head -n 5 || true)"
  if [[ -z "$lines" ]]; then
    lines="$(tail -n 5 "$log_file" 2>/dev/null || true)"
  fi

  if [[ -n "$lines" ]]; then
    printf '%s\n' "$lines" | sed 's/^/  /'
  fi
}

run_check() {
  local name="$1"
  local log_name="$2"
  shift 2

  if "$@" > "$LOG_DIR/$log_name" 2>&1; then
    status "$name" "пройдено"
  else
    failed=1
    status "$name" "не пройдено"
    summarize_failure "$LOG_DIR/$log_name"
  fi
}

# База задачи для диффа. Сравнение только с рабочим деревом пропускает всё, что
# уже закоммичено в ветку: на UPL-736 миграции лежали в коммите ветки, поэтому
# проверка миграций молча печатала «пропущено» и не влияла на exit (SB-117).
# Базу задаёт BASE_REF, иначе берётся первый существующий кандидат.
BASE_REF="${BASE_REF:-}"

detect_base_ref() {
  local candidate

  if [[ -n "$BASE_REF" ]]; then
    printf '%s' "$BASE_REF"
    return
  fi

  for candidate in origin/develop develop origin/main main; do
    if git rev-parse --verify --quiet "$candidate" >/dev/null 2>&1; then
      printf '%s' "$candidate"
      return
    fi
  done
}

collect_changed_files() {
  BASE_REF="$(detect_base_ref)"

  {
    [[ -n "$BASE_REF" ]] && git diff --name-only "$BASE_REF...HEAD"
    git diff --name-only
    git diff --cached --name-only
    git ls-files --others --exclude-standard
  } | sort -u > "$CHANGED_FILE" 2>"$LOG_DIR/changed-files.err"
}

needs_migration_check() {
  grep -Eq '(^|/)migrations?/.*\.(up|down)\.sql$' "$CHANGED_FILE"
}

# Построчный разбор вместо `source`: значение с пробелами исполняется как
# команда (`WEBSSO_SCOPES=openid sub phone profile` → «sub: command not found»),
# и до переменной не доходит — проверка идёт с пустым значением. Значение с
# glob-символом ещё и раскрывается по каталогу сервиса.
load_env_files() {
  local env_file line key value
  for env_file in "$SERVICE_ROOT/configs/.env" "$SERVICE_ROOT/.env"; do
    [[ -f "$env_file" ]] || continue
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
      [[ "$line" == *=* ]] || continue
      key="${line%%=*}"
      value="${line#*=}"
      key="${key#"${key%%[![:space:]]*}"}"
      key="${key#export }"
      key="${key%"${key##*[![:space:]]}"}"
      [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
      # Кавычки вокруг значения снимает и `source`, поведение сохраняем.
      if [[ ${#value} -ge 2 && ( "$value" == \"*\" || "$value" == \'*\' ) ]]; then
        value="${value:1:${#value}-2}"
      fi
      export "$key=$value"
    done < "$env_file"
  done
}

# Контейнер принадлежит стенду этого сервиса, если его compose-проект развёрнут
# из каталога сервиса. Без такой проверки общий кандидат `db` подхватывает
# контейнер чужого стенда, и временная БД проверки миграций создаётся в нём
# (SB-118): в docker ps станции обычно висит `db` соседнего сервиса.
container_belongs_to_service() {
  local work_dir
  work_dir="$(docker inspect "$1" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null)"
  # Равенство — не только вложенность: compose лежит и в deployments/compose,
  # и в корне сервиса (file-storage-service).
  [[ -n "$work_dir" && ( "$work_dir" == "$SERVICE_MAIN_ROOT" || "$work_dir" == "$SERVICE_MAIN_ROOT"/* ) ]]
}

detect_db_container() {
  local candidate

  docker ps --format '{{.Names}}' > "$LOG_DIR/docker-ps.log" 2>&1 || return 1

  if [[ -n "${DB_CONTAINER:-}" ]]; then
    grep -qx "$DB_CONTAINER" "$LOG_DIR/docker-ps.log" && printf '%s' "$DB_CONTAINER"
    return
  fi

  for candidate in "${SERVICE_NAME}-db" db; do
    if grep -qx "$candidate" "$LOG_DIR/docker-ps.log" && container_belongs_to_service "$candidate"; then
      printf '%s' "$candidate"
      return
    fi
  done
}

run_migration_check() {
  local log_file="$LOG_DIR/migration-up-down.log"
  local temp_db="${SERVICE_NAME//-/_}_final_checks_$(date +%Y%m%d%H%M%S)_$$"
  local migrations_dir="${MIGRATIONS_DIR:-migrations}"
  local db_user="${DB_USER:-postgres}"
  local db_container
  local env_candidate
  local base_env
  local temp_env
  local migrations_count
  local down_steps

  : > "$log_file"

  if [[ ! -d "$migrations_dir" ]]; then
    failed=1
    status "migration up/down" "блокер: каталог миграций не найден"
    return
  fi

  # Изолированную up/down-проверку делаем штатным `go run ./cmd/migrate`, а не
  # внешним golang-migrate CLI (SB-46): CLI в окружении может не быть, а cmd/migrate
  # всегда собирается из исходников проекта. Нет cmd/migrate — не блокер, а пропуск.
  if [[ ! -d cmd/migrate ]]; then
    status "migration up/down" "пропущено: cmd/migrate отсутствует в сервисе"
    return
  fi

  db_container="$(detect_db_container)"
  if [[ -z "$db_container" ]]; then
    failed=1
    status "migration up/down" "блокер: контейнер БД стенда ${SERVICE_NAME} не найден (кандидаты: \${DB_CONTAINER}, ${SERVICE_NAME}-db, db; контейнер чужого стенда отвергнут — подними стенд сервиса или задай DB_CONTAINER)"
    return
  fi

  migrations_count="$(find "$migrations_dir" -maxdepth 1 -type f -name '*.up.sql' | wc -l | tr -d ' ')"
  if [[ "$migrations_count" == "0" ]]; then
    failed=1
    status "migration up/down" "блокер: *.up.sql миграции не найдены"
    return
  fi

  # Откатываем не более трёх последних миграций: старые миграции не правим, а
  # полный откат упирается в их исторические дефекты и красит проверку на любой
  # задаче сервиса (SB-106).
  down_steps="${MIGRATION_DOWN_STEPS:-3}"
  if (( down_steps > migrations_count )); then
    down_steps="$migrations_count"
  fi

  if ! docker exec "$db_container" psql -U "$db_user" -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"$temp_db\";" >> "$log_file" 2>&1; then
    failed=1
    status "migration up/down" "не пройдено"
    summarize_failure "$log_file"
    return
  fi

  # cmd/migrate берёт БД из конфига CONFIG_PATH (env-override DB_NAME игнорируется —
  # CONFIG_PATH приоритетнее). Направляем на временную БД копией конфига с
  # подменённым DB_NAME, а не через окружение.
  # В worktree задачи configs/.env отсутствует (gitignored, из основного дерева не
  # копируется), а без него cmd/migrate падает на подключении — проверка давала бы
  # ложное «не пройдено». Берём конфиг основного рабочего дерева репозитория, а при
  # его отсутствии — блокер, чтобы причина не читалась как дефект миграций.
  base_env=""
  for env_candidate in "$SERVICE_ROOT/configs/.env" "$SERVICE_ROOT/.env" "$SERVICE_MAIN_ROOT/configs/.env" "$SERVICE_MAIN_ROOT/.env"; do
    if [[ -f "$env_candidate" ]]; then
      base_env="$env_candidate"
      break
    fi
  done

  if [[ -z "$base_env" ]]; then
    failed=1
    status "migration up/down" "блокер: конфиг БД не найден (configs/.env или .env в $SERVICE_ROOT, $SERVICE_MAIN_ROOT)"
    return
  fi

  temp_env="$LOG_DIR/migrate.env"
  sed "s/^DB_NAME=.*/DB_NAME=$temp_db/" "$base_env" > "$temp_env"
  grep -q '^DB_NAME=' "$temp_env" || printf 'DB_NAME=%s\n' "$temp_db" >> "$temp_env"

  if CONFIG_PATH="$temp_env" go run ./cmd/migrate -direction up >> "$log_file" 2>&1 &&
    CONFIG_PATH="$temp_env" go run ./cmd/migrate -direction down -steps "$down_steps" >> "$log_file" 2>&1; then
    status "migration up/down" "пройдено (up: все, down: последние $down_steps)"
  else
    failed=1
    status "migration up/down" "не пройдено"
    summarize_failure "$log_file"
  fi

  rm -f "$temp_env"
  docker exec "$db_container" psql -U "$db_user" -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS \"$temp_db\";" >> "$log_file" 2>&1 || true
}

check_testify_convention() {
  local log_file="$LOG_DIR/testify-convention.log"
  local violations=0

  : > "$log_file"
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    # Файлы без тестовых функций — HTTP-моки, фабрики, сид данных — ассертов не
    # содержат по определению, нарушением их считать нечего (SB-105).
    grep -qE '^func (Test|Benchmark|Fuzz)' "$f" || continue
    if ! grep -q 'stretchr/testify' "$f"; then
      violations=$((violations + 1))
      printf '%s\n' "$f" >> "$log_file"
    fi
  done < <(git ls-files '*_test.go')

  if [[ "$violations" -eq 0 ]]; then
    status "testify-convention" "пройдено"
  else
    failed=1
    status "testify-convention" "не пройдено: файлы без testify (см. profiles/go.md)"
    sed 's/^/  /' "$log_file"
  fi
}

if [[ -z "${AGENT_SKILLS_DIR:-}" || ! -d "$AGENT_SKILLS_DIR" ]]; then
  failed=1
  status "skills" "блокер: AGENT_SKILLS_DIR не задан или не существует"
else
  find -L "$AGENT_SKILLS_DIR" -maxdepth 4 -name SKILL.md > "$LOG_DIR/skills.log" 2>&1
  status "skills" "проверены"
fi

collect_changed_files
load_env_files
check_testify_convention

if make -n pre-commit >/dev/null 2>&1; then
  run_check "make pre-commit" "make-pre-commit.log" make pre-commit
else
  failed=1
  status "make pre-commit" "блокер: цель pre-commit не найдена в Makefile"
fi

shadow_tool="$(go env GOPATH)/bin/shadow"
if [[ -x "$shadow_tool" ]]; then
  run_check "shadow-vet" "shadow-vet.log" go vet -vettool="$shadow_tool" -strict ./...
else
  failed=1
  status "shadow-vet" "блокер: shadow не найден ($shadow_tool)"
fi

if needs_migration_check; then
  run_migration_check
else
  status "migration up/down" "пропущено: в диффе задачи нет миграций (база: ${BASE_REF:-не определена})"
fi

if [[ "$MODE" == "verify" ]]; then
  for relative_path in "${VERIFY_SEED_PATH_LIST[@]}"; do
    source_seed="$ORIGINAL_SERVICE_ROOT/$relative_path"
    verify_seed="$VERIFY_ROOT/$relative_path"
    if [[ ! -e "$source_seed" && ! -e "$verify_seed" ]]; then
      continue
    fi
    if [[ ! -e "$source_seed" || ! -e "$verify_seed" ]] \
      || ! diff -qr "$source_seed" "$verify_seed" >/dev/null 2>&1; then
      failed=1
      status "seeded artifact purity" "не пройдено: $relative_path изменён project checks"
    fi
  done
fi

git status --short > "$STATUS_FILE" 2>"$LOG_DIR/git-status.err"
if [[ -s "$STATUS_FILE" ]]; then
  status "git status --short" "есть изменения"
  if [[ "$MODE" == "verify" ]]; then
    failed=1
    status "candidate purity" "не пройдено: project checks изменили disposable snapshot"
  fi
else
  status "git status --short" "чисто"
fi

status "logs" "$LOG_DIR"

exit "$failed"
