#!/usr/bin/env bash
# Единая локальная инфраструктура UPL: общий compose-блок (db, redis,
# kafka, rabbitmq, minio, jaeger, envoy) + управление экземплярами сервисов
# на хосте, поимённо по ветке запуска.
#
# Экземпляры сервисов:
#   develop (без суффикса)  — постоянные, основной чекаут сервиса, канонический
#                             порт, общая БД без суффикса; рестарт только явно:
#                             svc down + svc up (автоцикла и слежения за кодом нет).
#   задачи (суффикс ключа)  — worktree services/<repo>-UPL-<N>, ветка
#                             feature/UPL-<N>, порт 9000+(порт-8000); БД <db>_<N>
#                             (клон основной) только у задач с миграциями, иначе
#                             общая БД; снимаются task close.
#
# Usage: upl-infra.sh <команда>
#   infra up|down|status [--with-keycloak]
#   svc up | svc down — up разворачивает экземпляры по текущим веткам чекаутов:
#                       develop → разв-экземпляр, feature/UPL-N → задачный из этого же
#                       каталога; down гасит разв-экземпляры, задачные — svc down --task;
#                       envoy (gateway) управляется теми же командами: up рендерит конфиг
#                       из чекаута/worktree задачи и держит контейнер compose
#   svc up|down|logs|status <svc> [--task UPL-XXXX | --branch <b> --suffix <N>] [--proc api|worker|consumer|poller]
#                       <svc> - go-сервисы и envoy; фоновый процесс (--proc consumer|worker|poller) -
#                       отдельный экземпляр <svc>.<proc>[-<N>] с портом health 10000+1000*i+(порт-8000);
#                       на стенде работает один экземпляр каждого фонового процесса (develop или задачи)
#   svc migrate <svc> [--task ...] up|down
#   svc switch [<branch>] [--fetch] — переключить основные чекауты; без аргумента
#                       или с develop|master|main каждый репозиторий встаёт на свою
#                       основную ветку: develop, если она есть, иначе канон —
#                       первая существующая из master/main; поднятые develop-
#                       экземпляры переключённых сервисов останавливаются;
#                       --fetch обновляет origin заранее; конфиг envoy рендерится
#                       из основной ветки или из worktree задачи
#   svc up --task UPL-XXXX — задачные экземпляры ветки сразу для всех сервисов,
#                       где она есть (чекаут или worktree), включая конфиг envoy
#   seed catalog
#   accept <UPL-XXXX> — поднимает экземпляры всех затронутых задачей сервисов:
#                       по worktree, живому экземпляру или task-ветке (стенд
#                       завершённой задачи поднимается из её ветки заново),
#                       и фоновые процессы, чей код меняет задача
#   task close <UPL-XXXX>
#   dev sync|check  — синхронизация/отчёт локальных БД с develop postgres (read-only)
#   doctor
#
# Смена задачных экземпляров (svc up/down --task, task close) сама перерендеривает
# env и перезапускает задетые экземпляры: разв- и уже поднятые задачные,
# чей адрес пира сменился.
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SELF="$SCRIPT_DIR/$(basename "$0")"
UPL_HOME="${UPL_HOME:-$HOME/_dev/git/obs/upl}"
RT="$UPL_HOME/.infra"
COMPOSE_FILE="$SCRIPT_DIR/compose.yaml"
export PATH="/usr/local/go/bin:$HOME/go/bin:/opt/homebrew/bin:$PATH"

die() { printf 'ошибка: %s\n' "$1" >&2; exit 1; }
info() { printf '%s\n' "$1"; }

# ── Реестр сервисов ──────────────────────────────────────────────────────────

# go-сервисы: хост-процессы с реестровым портом и опциональной БД.
KNOWN_SERVICES="auth user catalog report tracking ord bff"
# gateway-сервисы: контейнеры compose, код = отрендеренный конфиг (envoy).
GATEWAY_SERVICES="envoy"
ALL_SERVICES="$KNOWN_SERVICES $GATEWAY_SERVICES"

svc_repo() {
  case "$1" in
    auth) echo auth-service ;; user) echo user-service ;;
    catalog) echo catalog-service ;; report) echo report-service ;;
    tracking) echo tracking-service ;; ord) echo ord-service ;; bff) echo bff ;;
    envoy) echo envoy ;;
    *) return 1 ;;
  esac
}

svc_port() {
  case "$1" in
    auth) echo 8100 ;; user) echo 8101 ;; catalog) echo 8102 ;;
    report) echo 8103 ;; tracking) echo 8108 ;; ord) echo 8666 ;; bff) echo 8113 ;;
    envoy) echo 8110 ;;
  esac
}

svc_kind() { # go — хост-процесс; gateway — контейнер compose с рендер-конфигом
  case "$1" in
    auth|user|catalog|report|tracking|ord|bff) echo go ;;
    envoy) echo gateway ;;
    *) return 1 ;;
  esac
}

# Основные ветки в порядке приоритета: в репозитории с develop вся работа
# идёт с develop (master/main не используются); без develop — master, затем main.
svc_base_branches() {
  echo "develop master main"
}

svc_base_branch() { # <svc> → основная ветка: develop, если есть (local или origin), иначе первая из master/main
  local repo b
  repo="$UPL_HOME/services/$(svc_repo "$1")" || return 1
  for b in $(svc_base_branches "$1"); do
    if git -C "$repo" show-ref --verify -q "refs/heads/$b" \
      || git -C "$repo" show-ref --verify -q "refs/remotes/origin/$b"; then
      echo "$b"; return 0
    fi
  done
  return 1
}

# Пиры сервиса: чьи URL входят в его env (см. render_env). Используется
# refresh_peers для рестарта задетых экземпляров (разв- и задачных).
svc_peers() {
  case "$1" in
    auth) echo user ;;
    user) echo catalog ;;
    catalog|report) echo user ;;
    tracking) echo "user catalog" ;;
    ord) echo catalog ;;
    bff) echo "user catalog ord auth" ;;
    *) return 1 ;;
  esac
}

# Какой общий postgres держит основную БД сервиса (пусто — БД нет).
svc_dbhost() {
  case "$1" in
    auth|user|ord|catalog|report|tracking) echo db ;; bff|envoy) return 1 ;;
  esac
}

svc_dbname() {
  case "$1" in
    auth) echo auth ;; user) echo user ;; ord) echo ord ;;
    catalog) echo catalog ;; report) echo report ;; tracking) echo tracking ;;
  esac
}

# ── Состояние экземпляров ────────────────────────────────────────────────────

pidfile() { printf '%s/run/%s.pid' "$RT" "$1"; }
childfile() { printf '%s/run/%s.child' "$RT" "$1"; }
logfile() { printf '%s/log/%s.log' "$RT" "$1"; }
envfile() { printf '%s/env/%s.env' "$RT" "$1"; }

# PID в pidfile жив и действительно наш супервизор (защита от переиспользованного PID
# и от ложного совпадения имени-префикса: auth vs auth-1226).
pid_is_supervisor() { # <pid> <name>
  ps -p "$1" -o command= 2>/dev/null | grep -q "__supervisor $2 "
}

instance_running() {
  local f pid
  f="$(pidfile "$1")"
  [ -f "$f" ] || return 1
  pid="$(cat "$f" 2>/dev/null)"
  [ -n "$pid" ] && pid_is_supervisor "$pid" "$1"
}

# Порт экземпляра сервиса с учётом поднятых экземпляров задачи (они приоритетны).
live_port() {
  local p
  p="$(instance_task_port "$1")"
  [ -n "$p" ] && { echo "$p"; return; }
  echo "$(svc_port "$1")"
}

peer_url() { printf 'http://localhost:%s' "$(live_port "$1")"; }

port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | grep -q .; }

wait_for_listen() { # <port> [сек=20] — дожидается bind (go run компилирует первый бинарь до ~10 с)
  local port="$1" limit="${2:-20}" i
  for i in $(seq "$((limit * 10))"); do
    port_busy "$port" && return 0
    sleep 0.1
  done
  return 1
}

port_holder() { # <port> → "имя (ветка)" живого экземпляра с этим портом или пусто
  local f n base p srcdir br
  for f in "$RT"/run/*.pid; do
    [ -f "$f" ] || continue
    n="$(basename "$f" .pid)"
    case "$n" in *.*) continue ;; esac # фоновый процесс: свой порт, держателя называет bg_slot_holder
    instance_running "$n" || continue
    case "$n" in
      *-*) base="$(svc_port "${n%%-*}")"; p=$((9000 + base - 8000)); srcdir="$(worktree_path "${n%%-*}" "${n##*-}")" ;;
      *) p="$(svc_port "$n" 2>/dev/null)"; srcdir="$UPL_HOME/services/$(svc_repo "$n")" ;;
    esac
    [ "$p" = "$1" ] || continue
    br="$(git -C "$srcdir" branch --show-current 2>/dev/null || true)"
    [ -n "$br" ] || br="(detached)"
    echo "$n, ветка $br"
    return
  done
}

reap_orphans() { # <port> <repo> → 0, если после зачистки осиротевших бинарей сервиса порт свободен
  # Сирота = наш бинарь (api|worker|consumer|poller), чей cwd лежит в дереве
  # $UPL_HOME/services/<repo> (чекаут или worktree) и чей живой родитель не из
  # цепочки запуска (go run / супервизор). Чужие процессы не трогаются;
  # -9 только по подтверждённым кандидатам после неудачного TERM.
  local port="$1" repo="$2" pid name pids cwd ppid pcomm pcmd cand="" root
  root="$(cd "$UPL_HOME" 2>/dev/null && pwd -P)/services/"
  pids="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null)"
  [ -n "$pids" ] || return 0
  for pid in $pids; do
    name="$(ps -p "$pid" -o comm= 2>/dev/null)"; name="${name##*/}"
    case "$name" in
      api|worker|consumer|poller) ;;
      *) continue ;;
    esac
    cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')"
    case "$cwd" in
      "$root$repo"*) ;;
      *) continue ;;
    esac
    ppid="$(ps -p "$pid" -o ppid= 2>/dev/null | tr -d ' ')"
    if [ -n "$ppid" ] && [ "$ppid" != "0" ] && kill -0 "$ppid" 2>/dev/null; then
      pcomm="$(ps -p "$ppid" -o comm= 2>/dev/null)"; pcomm="${pcomm##*/}"
      pcmd="$(ps -p "$ppid" -o command= 2>/dev/null)"
      case "$pcomm" in
        go|compile) continue ;; # живой go run - экземпляр под присмотром
      esac
      case "$pcmd" in
        *"__supervisor"*) continue ;; # живой супервизор - то же
      esac
    fi
    cand="$cand $pid"
    info "порт $1 занимал осиротевший бинарь $name($pid) без супервизора — гашу"
    kill "$pid" 2>/dev/null
  done
  [ -n "$cand" ] || { lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | grep -q . && return 1; return 0; }
  for i in 1 2 3 4 5 6; do
    lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | grep -q . || return 0
    sleep 0.5
  done
  for pid in $cand; do
    kill -0 "$pid" 2>/dev/null || continue
    name="$(ps -p "$pid" -o comm= 2>/dev/null)"; name="${name##*/}"
    case "$name" in api|worker|consumer|poller) kill -9 "$pid" 2>/dev/null ;; esac
  done
  sleep 0.5
  lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | grep -q . && return 1
  return 0
}

# ── Окружение экземпляра ─────────────────────────────────────────────────────

env_set() { # <file> <key> <value>
  grep -v "^$2=" "$1" > "$1.tmp" 2>/dev/null
  printf '%s=%s\n' "$2" "$3" >> "$1.tmp"
  mv "$1.tmp" "$1"
}

env_get() { # <file> <key> → значение или пусто
  sed -n "s/^$2=//p" "$1" 2>/dev/null | tail -n 1
}

# Рендерит .env экземпляра из configs/.env.example выбранного дерева
# (задача - из worktree: обязательные ключи задачной конфигурации не теряются).
render_env() { # <svc> <src-dir> <env-path-out> <http-port> <db-name> [task 0|1]
  local svc="$1" src_dir="$2" out="$3" port="$4" dbname="$5" is_task="${6:-0}"
  # src локален обязательно: без local динамический скоупинг Bash перезапишет
  # src вызывающего cmd_svc_up путём до .env.example (регрессия 9997).
  local src dbh dbport
  # База - локальный configs/.env сервиса (реальные креды/ключи разработки);
  # инфраструктурные ключи (HTTP_PORT, DB_*, REDIS_*, ...) перезаписываются ниже.
  # Симлинк (auth: configs/.env → instance-env) локальным файлом не считается -
  # берём шаблон. Шаблон .env.example - fallback, когда локального .env нет.
  src="$src_dir/configs/.env"
  if [ ! -f "$src" ] || [ -L "$src" ]; then
    src="$src_dir/configs/.env.example"
  fi
  [ -f "$src" ] || die "$src не найден"
  cp "$src" "$out"
  dbh="$(svc_dbhost "$svc" 2>/dev/null || true)"
  dbport=5432
  env_set "$out" HTTP_PORT "$port"
  if [ -n "$dbh" ]; then
    env_set "$out" DB_HOST localhost
    env_set "$out" DB_PORT "$dbport"
    env_set "$out" DB_NAME "$dbname"
  fi
  env_set "$out" REDIS_HOST localhost
  env_set "$out" REDIS_PORT 6379
  env_set "$out" TRACING_HOST localhost
  env_set "$out" TRACING_PORT 4317
  env_set "$out" SWAGGER_ENABLED true
  case "$svc" in
    auth)
      env_set "$out" AUTH_SIGNING_KEY "${AUTH_SIGNING_KEY:-upl-local-dev-signing-key}"
      env_set "$out" DEV_LOGIN_ENABLED true
      env_set "$out" USER_SERVICE_BASE_URL "$(peer_url user)" ;;
    user)
      env_set "$out" KAFKA_BROKERS localhost:9094
      env_set "$out" CATALOG_SERVICE_BASE_URL "$(peer_url catalog)" ;;
    catalog)
      env_set "$out" KAFKA_BROKERS localhost:9094
      env_set "$out" USER_SERVICE_BASE_URL "$(peer_url user)"
      env_set "$out" RABBITMQ_ENABLED true
      env_set "$out" RABBITMQ_URL "amqp://guest:guest@localhost:5672/" ;;
    report)
      env_set "$out" KAFKA_BROKERS localhost:9094
      env_set "$out" USER_SERVICE_BASE_URL "$(peer_url user)" ;;
    tracking)
      env_set "$out" KAFKA_BROKERS localhost:9094
      env_set "$out" USER_SERVICE_BASE_URL "$(peer_url user)"
      env_set "$out" CATALOG_SERVICE_BASE_URL "$(peer_url catalog)" ;;
    ord)
      env_set "$out" CATALOG_SERVICE_BASE_URL "$(peer_url catalog)" ;;
    bff)
      env_set "$out" USER_SERVICE_URL "$(peer_url user)"
      env_set "$out" CATALOG_SERVICE_URL "$(peer_url catalog)"
      env_set "$out" ORD_SERVICE_URL "$(peer_url ord)"
      env_set "$out" AUTH_SERVICE_URL "$(peer_url auth)" ;;
  esac
}

# A1: env экземпляра фиксирует адреса пиров на старте. Смена задачных
# экземпляров (up/down/close) меняет live_port пира, и задетые экземпляры
# (те, чей env ссылается на изменённый сервис) перерендериваются и рестартуют.
refresh_peers() { # <svc>... — сервисы, чьи задачные экземпляры сменились
  [ $# -gt 0 ] || return 0
  local d c need tmp
  for d in $KNOWN_SERVICES; do
    instance_running "$d" || continue
    [ -f "$(envfile "$d")" ] || continue
    need=0
    for c in "$@"; do
      case " $(svc_peers "$d" 2>/dev/null) " in *" $c "*) need=1; break ;; esac
    done
    [ "$need" = "1" ] || continue
    # Кандидат рендерится тем же render_env: отличается — env устарел.
    tmp="$(mktemp -t upl-peer.XXXXXX)" || continue
    ( render_env "$d" "$UPL_HOME/services/$(svc_repo "$d")" "$tmp" "$(svc_port "$d")" "$(svc_dbname "$d")" 0 ) \
      || { rm -f "$tmp"; continue; }
    if cmp -s "$tmp" "$(envfile "$d")"; then rm -f "$tmp"; continue; fi
    rm -f "$tmp"
    info "$d: адрес пира сменился — перезапускаю с обновлённым env"
    stop_instance "$d"
    ( cmd_svc_up "$d" ) || info "$d: не поднялся после перерендера — подними вручную: svc up $d"
  done
  # Задачные экземпляры (STAND-TASK-PEER-ENV): подъём/снятие пир-экземпляра
  # задачи меняет live_port и для уже поднятых задачных
  # (подъём user-<N> при живых auth-<N>, report-<N>). Рендер-кандидат здесь
  # не воспроизвести: источник и БД задачного вычисляет только cmd_svc_up,
  # поэтому устаревание определяется по факту: в env нет текущего live-адреса
  # пира. Перезапуск идёт через cmd_svc_up, env перерендеривается целиком.
  local name sfx stale
  for d in $KNOWN_SERVICES; do
    for name in $(instance_names "$d"); do
      case "$name" in "$d"-*) ;; *) continue ;; esac
      instance_running "$name" || continue
      [ -f "$(envfile "$name")" ] || continue
      need=0; stale=0
      for c in "$@"; do
        case " $(svc_peers "$d" 2>/dev/null) " in *" $c "*)
          need=1
          grep -Eq "http://localhost:$(live_port "$c")([^0-9]|\$)" "$(envfile "$name")" || stale=1 ;;
        esac
      done
      { [ "$need" = "1" ] && [ "$stale" = "1" ]; } || continue
      sfx="${name##*-}"
      info "$name: адрес пира сменился — перезапускаю с обновлённым env"
      stop_instance "$name"
      ( cmd_svc_up "$d" --task "UPL-$sfx" ) \
        || info "$name: не поднялся после перерендера — подними вручную: svc up $d --task UPL-$sfx"
    done
  done
  # Фоновые процессы: их env несёт тех же пиров, что у api сервиса.
  # Устаревание - по факту: в env нет текущего live-адреса пира.
  local p
  for d in $KNOWN_SERVICES; do
    for name in $(bg_instance_names "$d"); do
      instance_running "$name" || continue
      [ -f "$(envfile "$name")" ] || continue
      need=0; stale=0
      for c in "$@"; do
        case " $(svc_peers "$d" 2>/dev/null) " in *" $c "*)
          need=1
          grep -Eq "http://localhost:$(live_port "$c")([^0-9]|\$)" "$(envfile "$name")" || stale=1 ;;
        esac
      done
      { [ "$need" = "1" ] && [ "$stale" = "1" ]; } || continue
      sfx="$(inst_sfx "$name")"; p="$(inst_proc "$name")"
      info "$name: адрес пира сменился - перезапускаю с обновлённым env"
      stop_instance "$name"
      if [ -n "$sfx" ]; then
        ( cmd_svc_up "$d" --task "UPL-$sfx" --proc "$p" ) \
          || info "$name: не поднялся после перерендера - подними вручную: svc up $d --task UPL-$sfx --proc $p"
      else
        ( cmd_svc_up "$d" --proc "$p" ) \
          || info "$name: не поднялся после перерендера - подними вручную: svc up $d --proc $p"
      fi
    done
  done
}

# ── БД ───────────────────────────────────────────────────────────────────────

psql_db() { # <dbhost> <dbname> <sql>
  docker exec "$1" psql -X -q -v ON_ERROR_STOP=1 -U postgres -d "$2" -c "$3"
}

db_exists() { # <dbhost> <dbname>
  [ "$(docker exec "$1" psql -X -tA -U postgres -d postgres -c "SELECT 1 FROM pg_database WHERE datname='$2'" 2>/dev/null | tr -d '[:space:]')" = "1" ]
}

ensure_db() { # <dbhost> <dbname> [источник-клона: БД создаётся пустой и наполняется дампом источника]
  db_exists "$1" "$2" && return 0
  docker exec "$1" psql -X -q -U postgres -d postgres -c "CREATE DATABASE \"$2\"" || die "не создана БД $2 на $1"
  if [ -n "$3" ]; then
    local dmp; dmp="$(mktemp -t upl-clone.XXXXXX)" || die "нет tmp для клона $2"
    docker exec "$1" pg_dump -U postgres --dbname="$3" >"$dmp" \
      || { rm -f "$dmp"; die "pg_dump $3 с $1 не прошёл"; }
    docker exec -i "$1" psql -X -q -v ON_ERROR_STOP=1 -U postgres --dbname="$2" <"$dmp" \
      || { rm -f "$dmp"; die "наполнение $2 из $3 не прошло"; }
    rm -f "$dmp"
  fi
}

drop_db() { # <dbhost> <dbname>
  docker exec "$1" psql -X -q -U postgres -d postgres -c "DROP DATABASE IF EXISTS \"$2\" WITH (FORCE)"
}

migrate_applied_version() { # <dbhost> <dbname> → число или 0
  docker exec "$1" psql -X -tA -U postgres -d "$2" -c "SELECT version FROM schema_migrations" 2>/dev/null | tr -d '[:space:]' || true
}

run_migrate() { # <src-dir> <env-file> up|down
  ( cd "$1" && CONFIG_PATH="$2" go run ./cmd/migrate -direction "$3" >/dev/null ) || die "миграции ($3) упали в $1"
}

max_migration() { # <src-dir> → номер последней up-миграции или 0
  ls "$1/migrations"/*.up.sql 2>/dev/null | sed 's!.*/!!;s/_.*//' | sort -n | tail -1
}

# ── Супервизор экземпляра ────────────────────────────────────────────────────

cmd_supervisor() { # __supervisor <name> <src> <env> <proc> — запуск одним процессом, без автоцикла
  local name="$1" src="$2" env="$3" proc="$4"
  local child rc hint
  # Своё имя процесса пишет сам супервизор: $! снаружи - pid обёртки nohup,
  # а не выжившего процесса, и pidfile с ним всегда показывает «остановлен».
  printf '%s\n' "$$" > "$(pidfile "$name")"
  trap ' [ -n "$child" ] && { pkill -P "$child" 2>/dev/null; kill "$child" 2>/dev/null; }; exit 0 ' TERM INT
  cd "$src" || exit 1
  export CONFIG_PATH="$env"
  printf '[%s] старт: go run ./cmd/%s\n' "$(date '+%H:%M:%S')" "$proc" >> "$(logfile "$name")"
  go run "./cmd/$proc" >> "$(logfile "$name")" 2>&1 &
  child=$!
  printf '%s' "$child" > "$(childfile "$name")"
  wait "$child"
  rc=$?
  hint="${name%%[.-]*}"
  case "$name" in *-*) hint="$hint --task UPL-${name##*-}" ;; esac
  [ "$proc" = "api" ] || hint="$hint --proc $proc"
  printf '[%s] процесс завершён (код %s) - автоперезапуска нет; подними заново: svc up %s\n' \
    "$(date '+%H:%M:%S')" "$rc" "$hint" >> "$(logfile "$name")"
  rm -f "$(pidfile "$name")" "$(childfile "$name")"
}

stop_instance() { # <name>
  local pid child i
  if [ -f "$(pidfile "$1")" ]; then
    pid="$(cat "$(pidfile "$1")")"
    if pid_is_supervisor "$pid" "$1"; then
      kill "$pid" 2>/dev/null
      for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
      kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
    fi
  fi
  if [ -f "$(childfile "$1")" ]; then
    child="$(cat "$(childfile "$1")")"
    pkill -P "$child" 2>/dev/null
    kill "$child" 2>/dev/null
  fi
  rm -f "$(pidfile "$1")" "$(childfile "$1")"
}

# ── Именование задачи ────────────────────────────────────────────────────────

task_suffix() { # ключ/ветка → числовой суффикс
  printf '%s' "$1" | sed 's/.*UPL-//;s/[^0-9].*//'
}

worktree_path() { # <svc> <sfx> → каталог worktree
  printf '%s/services/%s-UPL-%s' "$UPL_HOME" "$(svc_repo "$1")" "$2"
}

copy_local_env() { # <repo> <worktree> — локальные (gitignored) .env рядом с .env.example
  # Пути .env.example берём из worktree (его ветка может отличаться от чекаута),
  # сами .env - из чекаута: untracked-файлы git при создании worktree не переносит.
  local rel
  git -C "$2" ls-files | grep -E '(^|/)\.env\.example$' | while IFS= read -r rel; do
    if [ -f "$1/${rel%.env.example}.env" ]; then
      mkdir -p "$2/$(dirname "$rel")"
      cp "$1/${rel%.env.example}.env" "$2/${rel%.env.example}.env"
    fi
  done
}

git_worktree_add_local() { # <repo> <wt> <branch> — создаёт worktree и переносит локальные .env
  ( cd "$1" && git worktree add "$2" "$3" 2>/dev/null ) \
    || ( cd "$1" && git worktree add -b "$2" "$3" "origin/$3" 2>/dev/null ) \
    || return 1
  copy_local_env "$1" "$2"
}

# Есть ли у задачного дерева свои миграции относительно origin/develop; три точки:
# миграции, приехавшие ребейзом из develop, своими не считаются.
task_has_migrations() { # <worktree>
  [ -d "$1/migrations" ] || return 1
  git -C "$1" rev-parse -q --verify origin/develop >/dev/null 2>&1 \
    || die "$(basename "$1"): нет origin/develop — не отделить миграции задачи"
  [ -n "$(git -C "$1" diff --name-only origin/develop...HEAD -- migrations)" ]
}

instance_names() { # <svc> → имена живых/зафиксированных экземпляров (develop + задачи)
  local f base
  echo "$1"
  for f in "$RT"/run/"$1"-*.pid; do
    [ -e "$f" ] || continue
    base="$(basename "$f" .pid)"
    echo "$base"
  done
}

# Фоновые процессы сервиса (cmd/<proc>, кроме api) - отдельные экземпляры
# <svc>.<proc>[-<N>]: свой env, лог и порт health/metrics. Экземпляры одного
# процесса делят группы Kafka и данные, поэтому на стенде работает один
# экземпляр каждого фонового процесса - develop или одной задачи.
BG_PROCS="consumer worker poller"

instance_name() { # <svc> <sfx|""> <proc> -> имя: api - <svc>[-<sfx>], фоновый - <svc>.<proc>[-<sfx>]
  local n="$1"
  [ "$3" = "api" ] || n="$1.$3"
  [ -n "$2" ] && n="$n-$2"
  printf '%s' "$n"
}

inst_proc() { # <имя> -> процесс экземпляра
  local base="${1%%-*}"
  case "$base" in *.*) printf '%s' "${base#*.}" ;; *) printf 'api' ;; esac
}

inst_sfx() { # <имя> -> суффикс задачи или пусто
  case "$1" in *-*) printf '%s' "${1##*-}" ;; esac
}

proc_port() { # <svc> <proc> -> порт health/metrics фонового процесса: один на сервис и процесс
  local base idx
  base="$(svc_port "$1")"
  [ -n "$base" ] || return 1
  case "$2" in consumer) idx=0 ;; worker) idx=1 ;; poller) idx=2 ;; *) return 1 ;; esac
  echo $((10000 + 1000 * idx + base - 8000))
}

svc_bg_procs() { # <дерево> -> фоновые процессы, у которых есть cmd/<proc>
  local p
  for p in $BG_PROCS; do
    if [ -d "$1/cmd/$p" ]; then printf '%s ' "$p"; fi
  done
}

bg_instance_names() { # <svc> -> имена экземпляров фоновых процессов с pid-файлом
  local f
  for f in "$RT"/run/"$1".*.pid; do
    if [ -e "$f" ]; then basename "$f" .pid; fi
  done
}

bg_slot_holder() { # <svc> <proc> <имя> -> живой экземпляр того же фонового процесса, кроме <имя>
  local n
  for n in $(bg_instance_names "$1"); do
    [ "$n" = "$3" ] && continue
    [ "$(inst_proc "$n")" = "$2" ] || continue
    instance_running "$n" && { printf '%s' "$n"; return 0; }
  done
  return 1
}

stop_develop_bg() { # <svc> - гасит develop-экземпляры фоновых процессов: их код меняется вместе с чекаутом
  local n
  for n in $(bg_instance_names "$1"); do
    [ -z "$(inst_sfx "$n")" ] || continue
    instance_running "$n" || continue
    stop_instance "$n"
    info "svc switch: $n - экземпляр остановлен для обновления кода (подними: svc up $1 --proc $(inst_proc "$n"))"
  done
}

# ── Команды: infra ───────────────────────────────────────────────────────────

envoy_render_from() { # <src-dir> — config + standalone из чекаута, .eg (сертификаты) сохраняется
  local src="$1"
  [ -d "$src/config" ] || die "$src/config не найден"
  # backends.yaml — база из выбранной ветки (cp -R приносит её сам); удаляем
  # всё остальное, не трогая сам watched-файл (file provider видит удаление).
  # Порты живых экземпляров переписывает envoy_rewrite_backends_live.
  mkdir -p "$RT/envoy/config"
  find "$RT/envoy/config" -mindepth 1 ! -name backends.yaml -delete 2>/dev/null
  cp -R "$src/config/" "$RT/envoy/config/"
  cp "$src/standalone.yaml" "$RT/envoy/standalone.yaml"
  envoy_patch_standalone
  envoy_rewrite_backends_live
}

envoy_render_develop() { # рендер конфига envoy из основного чекаута services/envoy
  envoy_render_from "$UPL_HOME/services/envoy"
}

envoy_render_base() { # базовый конфиг envoy: чекаут на базе — из чекаута, иначе снапшот origin/<base>
  # Чекаут envoy switch держит только на базовой ветке; origin-снапшот страхует
  # ручные состояния чекаута вне базы.
  local repo="$UPL_HOME/services/envoy" base ref tmp
  base="$(svc_base_branch envoy)" || die "не найдена базовая ветка репозитория envoy"
  if [ "$(git -C "$repo" branch --show-current 2>/dev/null)" = "$base" ]; then
    envoy_render_from "$repo"
    return
  fi
  git -C "$repo" fetch -q origin 2>/dev/null || true
  ref="$base"
  git -C "$repo" show-ref --verify -q "refs/remotes/origin/$base" && ref="origin/$base"
  tmp="$UPL_HOME/services/envoy-UPL-0"
  if [ -d "$tmp" ]; then
    if ! git -C "$repo" worktree list --porcelain | grep -qF "$(cd "$tmp" 2>/dev/null && pwd -P)"; then
      [ -d "$tmp/config" ] && die "$tmp — не зарегистрирован как worktree, но содержит config; разберись вручную"
      mv "$tmp" "$tmp.idea-backup-$(date +%s)"
      info "envoy — незарегистрированный временный каталог перенесён в сторону"
    fi
  fi
  git_worktree_add_local "$repo" "$tmp" "$ref" \
    || die "не удалось получить базовый конфиг envoy из '$ref'"
  envoy_render_from "$tmp"
  ( cd "$repo" && git worktree remove --force "$tmp" >/dev/null 2>&1 ) || true
  info "envoy — применён базовый конфиг ($ref)"
}

envoy_patch_standalone() {
  sed -i '' -e 's|- \./config|- /workspace/config|' \
            -e 's|^      host: {}|      host:\n        configHome: /workspace/.eg\n        dataHome: /workspace/.eg|' \
    "$RT/envoy/standalone.yaml"
}

# Переписывает backends.yaml: 127.0.0.1 → host.docker.internal, порт каждого
# backend — порт живого экземпляра (задача приоритетна, иначе develop-канон).
# Пары "backend=порт" читает из файла $RT/envoy/.ports; нет пары — порт не меняется.
envoy_backends_python() {
  python3 - "$RT/envoy/config/backends.yaml" "$RT/envoy/.ports" <<'PY'
import os, re, sys
path, pairs_path = sys.argv[1], sys.argv[2]
ports = {}
for line in open(pairs_path):
    line = line.strip()
    if not line or "=" not in line:
        continue
    k, v = line.rsplit("=", 1)
    ports[k] = int(v)
text = open(path).read()
blocks = re.split(r"\n---\n", text)
out = []
for b in blocks:
    m = re.search(r"kind: Backend\nmetadata:\n  name: (\S+)", b)
    if not m:
        out.append(b)
        continue
    if m.group(1) in ports:
        port = ports[m.group(1)]
        b = re.sub(r"- (?:ip:\n\s+address: 127\.0\.0\.1|fqdn:\n\s+hostname: host\.docker\.internal)\n\s+port: \d+",
                   f"- fqdn:\n        hostname: host.docker.internal\n        port: {port}", b)
    else:
        b = re.sub(r"- ip:\n(\s+)address: 127\.0\.0\.1\n(\s+)port: (\d+)",
                   lambda mm: f"- fqdn:\n{mm.group(1)}hostname: host.docker.internal\n{mm.group(2)}port: {mm.group(3)}", b)
    out.append(b)
# Атомарная запись: envoy gateway file provider надёжно ловит create/rename
# нового inode, а модификацию существующего файла может не заметить.
tmp = path + ".tmp"
open(tmp, "w").write("\n---\n".join(out))
os.replace(tmp, path)
PY
}

compose() { # docker compose с обязательным UPL_ENVOY_DIR
  [ -d "$RT/envoy/config" ] || envoy_render_develop
  mkdir -p "$RT/envoy/.eg"
  UPL_ENVOY_DIR="$RT/envoy" docker compose -p upl-infra -f "$COMPOSE_FILE" "$@"
}

cmd_infra() {
  local action="${1:-}"; shift || true
  local extra=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --with-keycloak) extra="--profile keycloak" ;;
    esac
    shift
  done
  mkdir -p "$RT/env" "$RT/log" "$RT/run"
  case "$action" in
    up)
      compose up -d $extra || die "docker compose up упал"
      local i up
      for i in db redis; do
        up=""
        for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
          [ "$(docker inspect "$i" --format '{{.State.Health.Status}}' 2>/dev/null)" = "healthy" ] && { up=1; break; }
          sleep 1
        done
        [ -n "$up" ] || die "$i не стал healthy"
      done
      # Основные БД гарантируем и на существующем volume (initdb — только на пустом).
      docker exec -i db psql -X -q -U postgres -d postgres -f - < "$SCRIPT_DIR/initdb/01-databases.sql"
      # Расширение как на develop (без preload статистика не пишется, но схема совпадает).
      for i in $DEV_DBS; do
        docker exec db psql -X -q -U postgres -d "$i" -c 'CREATE EXTENSION IF NOT EXISTS pg_stat_statements WITH SCHEMA public' >/dev/null 2>&1
      done
      info "инфраструктура поднята: db(17):5432 redis:6379 kafka:9092/9094 rabbitmq:5672 minio:9000 jaeger:16686 envoy:8110"
      ;;
    down)
      compose down || die "docker compose down упал"
      info "инфраструктура остановлена (volumes сохранены; экземпляры сервисов на хосте не тронуты — останови их: svc down)"
      ;;
    status)
      compose ps
      ;;
    *) die "infra: неизвестное действие '$action' (up|down|status)" ;;
  esac
}

# ── Команды: svc ─────────────────────────────────────────────────────────────

cmd_svc_up_gateway() { # <svc> [task-key] [branch] — gateway в compose; «код» = отрендеренный конфиг
  local svc="$1" task_key="$2" branch="$3"
  docker ps --format '{{.Names}}' | grep -qx db || die "инфраструктура не поднята: upl-infra.sh infra up"
  local repo src sfx
  repo="$UPL_HOME/services/$(svc_repo "$svc")"
  src="$repo"
  if [ -n "$task_key" ]; then
    sfx="$(task_suffix "$task_key")"
    branch="${branch:-feature/UPL-$sfx}"
    # Чекаут gateway на задачную ветку не ставится: конфиг задачи - из worktree.
    # Чекаут, вручную оставленный на задачной ветке, используется как есть.
    if [ "$(git -C "$repo" branch --show-current 2>/dev/null)" = "$branch" ]; then
      src="$repo"
    else
      src="$(worktree_path "$svc" "$sfx")"
      if [ ! -d "$src" ]; then
        git_worktree_add_local "$repo" "$src" "$branch" \
          || die "нет ветки $branch у $(svc_repo "$svc") — создай её или проверь ключ"
      fi
    fi
  fi
  [ -d "$src/config" ] || die "$src/config не найден — конфиг $svc не из чего рендерить"
  envoy_render_from "$src"
  if ! docker ps --format '{{.Names}}' | grep -qx "$svc"; then
    compose up -d "$svc" || die "$svc не поднялся"
  fi
  info "$svc поднят (compose): порт $(svc_port "$svc"), конфиг из $(basename "$src")"
}

cmd_svc_up() { # <svc> [task-key|branch/suffix opts] [proc]
  local svc="$1"; shift
  local task_key="" branch="" sfx="" proc="api"
  while [ $# -gt 0 ]; do
    case "$1" in
      --task) task_key="$2"; shift 2 ;;
      --branch) branch="$2"; shift 2 ;;
      --suffix) sfx="$2"; shift 2 ;;
      --proc) proc="$2"; shift 2 ;;
      *) die "svc up: неизвестный флаг $1" ;;
    esac
  done
  if [ -z "$task_key" ] && [ -n "$sfx" ]; then task_key="UPL-$sfx"; fi
  if [ "$(svc_kind "$svc")" = "gateway" ]; then
    cmd_svc_up_gateway "$svc" "$task_key" "$branch"
    return
  fi
  case "$proc" in
    api) ;;
    *) proc_port "$svc" "$proc" >/dev/null || die "svc up: неизвестный процесс '$proc' (api|consumer|worker|poller)" ;;
  esac
  command -v go >/dev/null || die "go не найден в PATH (/usr/local/go/bin)"
  docker ps --format '{{.Names}}' | grep -qx db || die "инфраструктура не поднята: upl-infra.sh infra up"

  local repo src name port dbname env inst_task=0 own_db=0 inst_local=0
  repo="$UPL_HOME/services/$(svc_repo "$svc")" || die "неизвестный сервис $svc"
  # Без явного --task/--branch экземпляр следует ветке чекаута:
  # develop → разв-экземпляр; feature/UPL-N → задачный из этого же каталога.
  local cur
  cur="$(git -C "$repo" branch --show-current 2>/dev/null || true)"
  if [ -z "${task_key:-}${sfx:-}${branch:-}" ] \
    && [[ "$cur" =~ ^[Ff]eature/[Uu][Pp][Ll]-([0-9]+)$ ]]; then
    sfx="${BASH_REMATCH[1]}"
    task_key="UPL-$sfx"
    branch="$cur"
    inst_task=1
    inst_local=1
  fi
  if [ -n "${task_key:-}${sfx:-}" ]; then
    [ -z "$sfx" ] && sfx="$(task_suffix "$task_key")"
    [ -n "$sfx" ] || die "svc up: не распознал суффикс задачи"
    [ -n "$branch" ] || branch="feature/UPL-$sfx"
    inst_task=1
  fi
  # Запрошенная ветка совпадает с чекаутом (регистр различается на APFS-алиасах
  # refs): worktree невозможен, экземпляр поднимается из самого чекаута.
  if [ "$inst_task" = "1" ] && [ "$inst_local" = "0" ] \
    && [ "$(printf '%s' "$branch" | tr '[:upper:]' '[:lower:]')" \
      = "$(printf '%s' "$cur" | tr '[:upper:]' '[:lower:]')" ]; then
    inst_local=1
  fi
  if [ "$inst_task" = "1" ]; then
    name="$svc-$sfx"
    local base; base="$(svc_port "$svc")"
    port=$((9000 + base - 8000))
    local dn; dn="$(svc_dbname "$svc" 2>/dev/null || true)"
    dbname=""; [ -n "$dn" ] && dbname="${dn}_${sfx}"
    src="$(worktree_path "$svc" "$sfx")"
  else
    name="$svc"
    port="$(svc_port "$svc")"
    dbname="$(svc_dbname "$svc")"
    src="$repo"
    [ "$cur" = "develop" ] || die "$svc: ветка '$cur' — не develop и не задачная (feature/UPL-N); переключи чекаут (svc switch) или подними явно через --task"
  fi
  # Фоновый процесс - отдельный экземпляр со своим портом health/metrics.
  if [ "$proc" != "api" ]; then
    if [ "$inst_task" = "1" ]; then name="$(instance_name "$svc" "$sfx" "$proc")"; else name="$(instance_name "$svc" "" "$proc")"; fi
    port="$(proc_port "$svc" "$proc")"
  fi
  # Занятость проверяем до любых side effects (worktree, клон БД, env, миграции):
  # чужой экземпляр и его БД не трогаются, отказ дешёвый
  # (протокол параллельной работы - environment.md).
  if instance_running "$name"; then
    info "$name уже запущен (порт $port) — автоперезапуска нет; для нового кода: svc down + svc up"
    return 0
  fi
  if [ "$proc" != "api" ]; then
    local bg_holder
    bg_holder="$(bg_slot_holder "$svc" "$proc" "$name")" \
      && die "$name: процесс $svc.$proc уже работает как $bg_holder - экземпляры одного процесса делят группы Kafka и данные; жди его остановки или договаривайся"
  fi
  if port_busy "$port"; then
    local holder; holder="$(port_holder "$port")"
    if [ -n "$holder" ]; then
      die "порт $port занят экземпляром $holder — чужой экземпляр не трогается; жди его остановки или согласуй владельца"
    fi
    if ! reap_orphans "$port" "$(svc_repo "$svc")"; then
      die "порт $port занят посторонним процессом ($(lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR==2{print $1"("$2")"}')) — экземпляр $name не поднят"
    fi
  fi
  if [ "$inst_task" = "1" ]; then
    src="$(worktree_path "$svc" "$sfx")"
    if [ ! -d "$src" ]; then
      if [ "$inst_local" = "1" ]; then
        src="$repo" # worktree ещё нет — задачный экземпляр из самого чекаута: ветка уже в нём
      else
        git_worktree_add_local "$repo" "$src" "$branch" \
          || die "нет ветки $branch у $(svc_repo "$svc") — создай её или проверь ключ"
      fi
    fi
    local dbh; dbh="$(svc_dbhost "$svc" 2>/dev/null || true)"
    # БД задачи - только у задачи с миграциями (diff против origin/develop); создаётся
    # клоном основной БД. Задача без миграций работает на общей БД (environment.md).
    if [ -n "$dbh" ] && task_has_migrations "$src"; then
      ensure_db "$dbh" "$dbname" "$dn"
      own_db=1
    else
      dbname="$dn"
    fi
  fi
  [ -d "$src/cmd/$proc" ] || die "$name: в $(basename "$src") нет cmd/$proc"
  # Удалённый отслеживаемый .env.example восстанавливаем сами (render_env без него
  # невозможен); локальные правки существующего файла не трогаем.
  if [ ! -f "$src/configs/.env.example" ] \
    && git -C "$src" ls-files --error-unmatch configs/.env.example >/dev/null 2>&1; then
    git -C "$src" checkout -- configs/.env.example
    info "$name: восстановлен configs/.env.example (был удалён в чекауте)"
  fi
  # env рендерим до миграций: мигать можно только с проверенным CONFIG_PATH
  # (environment.md), общий рендер даёт хост/порт/имя БД этого экземпляра.
  render_env "$svc" "$src" "$(envfile "$name")" "$port" "$dbname" "$inst_task"
  if [ "$proc" != "api" ]; then
    local k
    for k in WORKER_PORT CONSUMER_PORT POLLER_PORT; do env_set "$(envfile "$name")" "$k" "$port"; done
  fi
  # Миграции на общем инстансе - только у владельца БД: develop-экземпляр или задачный
  # со своей <db>_<N>; миграции задачного дерева на общей БД запрещены (environment.md).
  if [ -n "$(svc_dbhost "$svc" 2>/dev/null || true)" ] && [ -d "$src/cmd/migrate" ] \
    && { [ "$inst_task" = "0" ] || [ "$own_db" = "1" ]; }; then
    run_migrate "$src" "$(envfile "$name")" up
    local want have
    want="$(max_migration "$src")"; have="$(migrate_applied_version "$(svc_dbhost "$svc")" "$dbname")"
    [ "$((10#${have:-0}))" -ge "$((10#${want:-0}))" ] || info "предупреждение: schema_migrations=$have, максимум миграций=$want ($dbname)"
  fi
  [ "$svc" = "auth" ] && { [ -e "$src/configs/.env" ] || ln -s "$(envfile "$name")" "$src/configs/.env"; }
  nohup bash "$SELF" __supervisor "$name" "$src" "$(envfile "$name")" "$proc" >/dev/null 2>&1 &
  disown 2>/dev/null
  sleep 1 # супервизор успел записать свой pid в pidfile
  # A4: "поднят" честно - после bind порта (у фонового процесса - порта health), а не после факта старта go run.
  wait_for_listen "$port" 20 \
    || info "предупреждение: $name не слушает порт $port за 20 с — go run ещё компилирует или упал; лог: $(logfile "$name")"
  # Адреса шлюза и пиров зависят только от api-экземпляров.
  if [ "$proc" = "api" ]; then
    if docker ps --format '{{.Names}}' | grep -qx envoy; then
      envoy_rewrite_backends_live # живые инстансы поменялись — file provider перечитает
    fi
    [ "$inst_task" = "1" ] && refresh_peers "$svc" # A1: разв- и поднятые задачные на новый адрес пира
  fi
  info "$name поднят: порт $port${dbname:+, БД $dbname}, лог $(logfile "$name")"
}

cmd_svc_down() {
  local svc="$1"; shift
  local sfx="" proc="api"
  while [ $# -gt 0 ]; do
    case "$1" in
      --task) sfx="$(task_suffix "$2")"; shift 2 ;;
      --proc) proc="$2"; shift 2 ;;
      *) shift ;;
    esac
  done
  if [ "$(svc_kind "$svc")" = "gateway" ]; then
    if docker ps --format '{{.Names}}' | grep -qx "$svc"; then
      compose stop "$svc" >/dev/null || die "$svc (compose) не остановился"
      info "$svc остановлен (compose)"
    else
      info "$svc не запущен (compose)"
    fi
    return
  fi
  local name; name="$(instance_name "$svc" "$sfx" "$proc")"
  stop_instance "$name"
  if [ "$proc" = "api" ]; then
    if docker ps --format '{{.Names}}' | grep -qx envoy; then
      envoy_rewrite_backends_live
    fi
    [ -n "$sfx" ] && refresh_peers "$svc" # A1: пиры возвращаются на develop-адрес
  fi
  info "$name остановлен"
}

cmd_svc_up_all() { # разв-экземпляры всех сервисов; отказ одного не мешает остальным
  local failed="" svc
  for svc in $ALL_SERVICES; do
    ( cmd_svc_up "$svc" ) || failed="$failed $svc"
  done
  [ -z "$failed" ] || die "не поднялись:$failed — причины выше; остальные экземпляры работают"
}

cmd_svc_up_branch() { # <branch> — задачные экземпляры ветки для всех сервисов, где она есть
  local branch="$1" svc repo failed=""
  [ -n "$(task_suffix "$branch")" ] || die "svc up: '$branch' — не задачная ветка (feature/UPL-N)"
  for svc in $ALL_SERVICES; do
    repo="$UPL_HOME/services/$(svc_repo "$svc")"
    if [ ! -d "$repo" ]; then
      info "svc up: $svc — чекаута нет ($repo), пропущен"; failed="$failed $svc"; continue
    fi
    # ветка доступна: это текущий чекаут, локальная ветка или есть на origin
    if [ "$(git -C "$repo" branch --show-current 2>/dev/null)" = "$branch" ] \
      || git -C "$repo" show-ref --verify -q "refs/heads/$branch" \
      || git -C "$repo" show-ref --verify -q "refs/remotes/origin/$branch"; then
      ( cmd_svc_up "$svc" --task "UPL-$(task_suffix "$branch")" --branch "$branch" ) || failed="$failed $svc"
    else
      info "svc up: $svc — ветки '$branch' нет, пропущен"
    fi
  done
  [ -z "$failed" ] || die "не поднялись:$failed — причины выше; остальные экземпляры работают"
}

cmd_svc_down_all() { # гасит develop-экземпляры всех сервисов; задачные экземпляры не трогает
  local svc any=0
  for svc in $ALL_SERVICES; do
    if [ "$(svc_kind "$svc")" = "gateway" ]; then
      if docker ps --format '{{.Names}}' | grep -qx "$svc"; then
        compose stop "$svc" >/dev/null || die "$svc (compose) не остановился"
        info "$svc остановлен (compose)"
        any=1
      fi
      continue
    fi
    if instance_running "$svc"; then
      stop_instance "$svc"
      info "$svc остановлен"
      any=1
    else
      stop_instance "$svc" # чистка устаревших pid-файлов
    fi
    local n
    for n in $(bg_instance_names "$svc"); do
      [ -z "$(inst_sfx "$n")" ] || continue
      if instance_running "$n"; then
        stop_instance "$n"
        info "$n остановлен"
      else
        stop_instance "$n" # чистка устаревших pid-файлов
      fi
    done
  done
  if [ "$any" = "1" ] && docker ps --format '{{.Names}}' | grep -qx envoy; then
    envoy_rewrite_backends_live
  fi
  if [ "$any" = "0" ]; then
    info "запущенных develop-экземпляров нет"
  fi
}

cmd_svc_switch() { # [<branch>] [--fetch] — переключить основные чекауты; без аргумента — базовые ветки
  local target="" do_fetch="" pos=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --fetch) do_fetch=1 ;;
      -*) die "svc switch: неизвестный флаг $1" ;;
      *) pos=$((pos + 1)); target="$1" ;;
    esac
    shift
  done
  [ "$pos" -le 1 ] || die "svc switch: максимум одна ветка"
  [ -n "$target" ] || target="develop"
  local svc repo cur tgt out failed="" kind base isbase
  for svc in $ALL_SERVICES; do
    repo="$UPL_HOME/services/$(svc_repo "$svc")"
    kind="$(svc_kind "$svc")"
    if [ ! -d "$repo" ]; then
      info "svc switch: $svc — чекаута нет ($repo), пропущен"; failed="$failed $svc"; continue
    fi
    if [ -n "$do_fetch" ]; then
      git -C "$repo" fetch --quiet origin 2>/dev/null \
        || info "svc switch: $svc — fetch не удался, ищу ветку по локальным ссылкам"
    fi
    # develop|master|main — запрос основной ветки репозитория. Основная ветка
    # одна: develop, если есть, иначе канон (первая существующая из master/main).
    # Правило простое и считается на каждом запуске (stateless).
    base="$(svc_base_branch "$svc" 2>/dev/null || true)"
    isbase=0
    case "$target" in
      develop|master|main)
        isbase=1
        if [ -n "$base" ]; then
          tgt="$base"
          [ "$tgt" != "$target" ] && info "svc switch: $svc — основная ветка $tgt ('$target' не используется)"
        else
          info "svc switch: $svc — нет ни develop, ни master/main ($(svc_base_branches "$svc"))"
          failed="$failed $svc"; continue
        fi ;;
      *)
        tgt="$target"
        if ! git -C "$repo" show-ref --verify -q "refs/heads/$tgt" \
          && ! git -C "$repo" show-ref -q --verify "refs/remotes/origin/$tgt"; then
          info "svc switch: $svc — ветки '$target' нет, цель ${base:-develop}"
          tgt="${base:-develop}"
          isbase=1
        fi ;;
    esac
    # Чекаут gateway держится только на базовой ветке; задачный конфиг - из worktree.
    if [ "$kind" = "gateway" ] && [ "$isbase" = "0" ]; then
      gateway_render_task "$svc" "$repo" "$tgt"
      continue
    fi
    if [ "$isbase" = "1" ]; then
      git -C "$repo" fetch -q origin 2>/dev/null || true
    fi
    cur="$(git -C "$repo" branch --show-current)"
    if [ "$cur" = "$tgt" ]; then
      # Уже на цели: базовую ветку догоняем ff-only; экземпляр останавливаем
      # только при реальной смене HEAD (иначе гонял бы старый бинарь).
      local moved=0 sha0 sha1
      if [ "$isbase" = "1" ] && [ -z "$(git -C "$repo" status --porcelain)" ]; then
        sha0="$(git -C "$repo" rev-parse "$tgt" 2>/dev/null)"
        if git -C "$repo" merge --ff-only "origin/$tgt" >/dev/null 2>&1; then
          sha1="$(git -C "$repo" rev-parse "$tgt" 2>/dev/null)"
          if [ "$sha0" != "$sha1" ]; then
            moved=1
            info "svc switch: $svc — $tgt догнан до $(git -C "$repo" log -1 --format=%h)"
          fi
        elif [ "$sha0" = "$(git -C "$repo" rev-parse "origin/$tgt" 2>/dev/null)" ]; then
          : # уже актуален
        else
          info "svc switch: $svc — $tgt расходится с origin, догон пропущен"
        fi
      fi
      if [ "$moved" = "1" ] && [ "$kind" = "go" ] && instance_running "$svc"; then
        stop_instance "$svc"
        docker ps --format '{{.Names}}' | grep -qx envoy && envoy_rewrite_backends_live
        info "svc switch: $svc — экземпляр остановлен для обновления кода (подними: svc up $svc)"
      elif [ "$moved" = "0" ]; then
        info "svc switch: $svc уже на '$tgt'"
      fi
      [ "$moved" = "1" ] && [ "$kind" = "go" ] && stop_develop_bg "$svc"
      if [ "$kind" = "gateway" ]; then
        envoy_render_from "$repo"
        info "switch: $svc — применён конфиг из чекаута ('$tgt')"
      fi
      continue
    fi
    if [ -n "$(git -C "$repo" status --porcelain)" ]; then
      info "svc switch: $svc — каталог не чист, не переключён (разберись вручную)"
      failed="$failed $svc"; continue
    fi
    # Останавливаем ДО switch: смена кода каталога под работающим экземпляром
    # оставила бы его на старом бинаре (автоцикла нет).
    local stopped=""
    if [ "$kind" = "go" ] && instance_running "$svc"; then
      stop_instance "$svc"
      if docker ps --format '{{.Names}}' | grep -qx envoy; then envoy_rewrite_backends_live; fi
      stopped=1
      info "svc switch: $svc — экземпляр остановлен перед switch"
    fi
    [ "$kind" = "go" ] && stop_develop_bg "$svc"
    if ! out="$(git -C "$repo" switch "$tgt" 2>&1)"; then
      info "svc switch: $svc — git switch '$tgt' отклонил:"
      printf '%s\n' "$out" | sed 's/^/    /'
      [ -n "$stopped" ] && info "svc switch: $svc — экземпляр остался остановлен; после разбора: svc up $svc"
      failed="$failed $svc"; continue
    fi
    # После switch на базовую ветку сразу догоняем её до origin.
    if [ "$isbase" = "1" ] && git -C "$repo" merge --ff-only "origin/$tgt" >/dev/null 2>&1; then
      info "svc switch: $svc — $tgt догнан до $(git -C "$repo" log -1 --format=%h)"
    fi
    info "svc switch: $svc → '$tgt'"
    if [ "$kind" = "gateway" ]; then
      envoy_render_from "$repo"
      info "switch: $svc — применён конфиг из чекаута ('$tgt')"
    fi
  done
  [ -z "$failed" ] || die "не переключены:$failed — причины выше; остальные переключены"
}

gateway_render_task() { # <svc> <repo> <task-branch> — конфиг задачи из worktree; чекаут не переключается
  local svc="$1" repo="$2" tgt="$3" sfx ewt ebr want got ewtres ebak
  sfx="$(task_suffix "$tgt")"
  ewt="$UPL_HOME/services/$(svc_repo "$svc")-UPL-$sfx"
  if [ "$(git -C "$repo" branch --show-current 2>/dev/null)" = "$tgt" ]; then
    envoy_render_from "$repo"
    info "switch: $svc — чекаут уже на '$tgt' (ручное состояние), конфиг применён из чекаута"
    return
  fi
  if [ -d "$ewt" ]; then
    ewtres="$(cd "$ewt" 2>/dev/null && pwd -P)"
    if ! git -C "$repo" worktree list --porcelain | grep -qF "worktree $ewtres"; then
      if [ -d "$ewt/config" ]; then
        die "switch: $ewt — не зарегистрирован как worktree, но содержит config; разберись вручную"
      fi
      ebak="$ewt.idea-backup-$(date +%s)"
      mv "$ewt" "$ebak"
      info "switch: $svc — незарегистрированный каталог перенесён в $(basename "$ebak")"
    fi
  fi
  if [ -d "$ewt" ]; then
    ebr="$(git -C "$ewt" branch --show-current 2>/dev/null)"
    if [ -n "$ebr" ] && [ "$ebr" != "$tgt" ]; then
      die "switch: $ewt — на ветке '$ebr', а требуется '$tgt'; разберись вручную"
    fi
    if [ -z "$ebr" ]; then
      # detached-worktree из origin: сличаем HEAD с требуемой веткой
      want="$(git -C "$repo" rev-parse --verify -q "refs/heads/$tgt" || git -C "$repo" rev-parse --verify -q "refs/remotes/origin/$tgt")"
      got="$(git -C "$ewt" rev-parse HEAD 2>/dev/null)"
      [ "$want" = "$got" ] || die "switch: $ewt — detached не на '$tgt'; разберись вручную"
    fi
  else
    git_worktree_add_local "$repo" "$ewt" "$tgt" \
      || die "switch: не удалось создать worktree $svc для '$tgt'"
  fi
  envoy_render_from "$ewt"
  info "switch: $svc — конфиг применён из worktree задачи"
}

cmd_svc_status() {
  local svc line inst pid child port gbranch gstate
  printf '%-24s %-6s %-8s %-18s %s\n' "INSTANCE" "PORT" "PID" "BRANCH" "STATE"
  for svc in $ALL_SERVICES; do
    if [ "$(svc_kind "$svc")" = "gateway" ]; then
      gbranch="$(git -C "$UPL_HOME/services/$(svc_repo "$svc")" branch --show-current 2>/dev/null || true)"
      [ -n "$gbranch" ] || gbranch="(detached)"
      gstate="остановлен"; docker ps --format '{{.Names}}' | grep -qx "$svc" && gstate="запущен (compose)"
      printf '%-24s %-6s %-8s %-18s %s\n' "$svc" "$(svc_port "$svc")" "-" "$gbranch" "$gstate"
      continue
    fi
    for inst in $(instance_names "$svc"); do
      local base srcdir branch; base="$(svc_port "$svc")"
      port="$base"; case "$inst" in "$svc"-*) port=$((9000 + base - 8000));; esac
      if [ "$inst" = "$svc" ]; then
        srcdir="$UPL_HOME/services/$(svc_repo "$svc")"
      else
        srcdir="$(worktree_path "$svc" "${inst##*-}")"
      fi
      branch="-"
      if [ -d "$srcdir" ]; then
        branch="$(git -C "$srcdir" branch --show-current 2>/dev/null || true)"
        [ -n "$branch" ] || branch="(detached)"
      fi
      if instance_running "$inst"; then
        pid="$(cat "$(pidfile "$inst")")"
        printf '%-24s %-6s %-8s %-18s %s\n' "$inst" "$port" "$pid" "$branch" "запущен"
      else
        printf '%-24s %-6s %-8s %-18s %s\n' "$inst" "$port" "-" "$branch" "остановлен"
      fi
    done
    for inst in $(bg_instance_names "$svc"); do
      local bsfx bsrc bbranch bport
      bsfx="$(inst_sfx "$inst")"
      bsrc="$UPL_HOME/services/$(svc_repo "$svc")"
      [ -n "$bsfx" ] && bsrc="$(worktree_path "$svc" "$bsfx")"
      bport="$(proc_port "$svc" "$(inst_proc "$inst")")"
      bbranch="-"
      if [ -d "$bsrc" ]; then
        bbranch="$(git -C "$bsrc" branch --show-current 2>/dev/null || true)"
        [ -n "$bbranch" ] || bbranch="(detached)"
      fi
      if instance_running "$inst"; then
        printf '%-24s %-6s %-8s %-18s %s\n' "$inst" "$bport" "$(cat "$(pidfile "$inst")")" "$bbranch" "запущен"
      else
        printf '%-24s %-6s %-8s %-18s %s\n' "$inst" "$bport" "-" "$bbranch" "остановлен"
      fi
    done
  done
}

cmd_svc_logs() {
  local svc="$1"; shift; local sfx="" proc="api"
  while [ $# -gt 0 ]; do case "$1" in --task) sfx="$(task_suffix "$2")"; shift 2;; --proc) proc="$2"; shift 2;; *) shift;; esac; done
  if [ "$(svc_kind "$svc")" = "gateway" ]; then
    docker logs --tail 200 -f "$svc"
    return
  fi
  local name; name="$(instance_name "$svc" "$sfx" "$proc")"
  tail -n 200 -f "$(logfile "$name")"
}

cmd_svc_migrate() { # <svc> up|down [--task ...]
  local svc="$1"; shift
  local dir="up" sfx=""
  while [ $# -gt 0 ]; do
    case "$1" in up|down) dir="$1"; shift ;; --task) sfx="$(task_suffix "$2")"; shift 2 ;; *) shift ;; esac
  done
  local repo name dbname src
  repo="$UPL_HOME/services/$(svc_repo "$svc")"
  name="$svc"; [ -n "$sfx" ] && { name="$svc-$sfx"; dbname="$(svc_dbname "$svc")_$sfx"; src="$(worktree_path "$svc" "$sfx")"; } || { dbname="$(svc_dbname "$svc")"; src="$repo"; }
  [ -n "$(svc_dbhost "$svc" 2>/dev/null || true)" ] || { info "$svc без БД - миграций нет"; return 0; }
  [ -d "$src/cmd/migrate" ] || { info "$svc: cmd/migrate отсутствует - пропуск"; return 0; }
  [ -f "$(envfile "$name")" ] || die "нет env экземпляра $name — подними его: svc up $svc"
  # Gate по факту, а не только по дереву: env бывает поднят до появления миграций
  # задачи и указывает на общую БД (environment.md).
  if [ -n "$sfx" ]; then
    local want_db have_db
    want_db="$(svc_dbname "$svc")_$sfx"
    have_db="$(env_get "$(envfile "$name")" DB_NAME)"
    if [ "$have_db" != "$want_db" ]; then
      if task_has_migrations "$src"; then
        die "env $name указывает на БД '${have_db:-нет}', а не на свою $want_db — конфигурация поднята до появления миграций; подними экземпляр заново: svc up $svc --task UPL-$sfx"
      fi
      die "экземпляр $name не владеет БД (env указывает на ${have_db:-нет}) — миграции задачного дерева на общей БД запрещены; изолированный прогон - <key>_check, применение к основной БД - task close UPL-$sfx"
    fi
  fi
  if [ "$dir" = "down" ]; then
    ( cd "$src" && CONFIG_PATH="$(envfile "$name")" go run ./cmd/migrate -direction down -steps 1 ) || die "откат не прошёл"
  else
    run_migrate "$src" "$(envfile "$name")" up
  fi
  info "migrate $dir выполнен для $name (БД $dbname)"
}

# ── Envoy под задачу / приёмка ───────────────────────────────────────────────

envoy_rewrite_backends_live() { # pairs backend=порт: задача приоритетна, иначе живой develop
  local svc name port
  : > "$RT/envoy/.ports"
  for svc in $KNOWN_SERVICES; do
    port="$(instance_task_port "$svc")"
    if [ -n "$port" ]; then
      printf '%s=%s\n' "$svc" "$port" >> "$RT/envoy/.ports"
    elif instance_running "$svc"; then
      printf '%s=%s\n' "$svc" "$(svc_port "$svc")" >> "$RT/envoy/.ports"
    fi
  done
  envoy_backends_python
}

instance_task_port() { # <svc> → порт живого экземпляра задачи или пусто
  local name base
  base="$(svc_port "$1")"
  for name in $(instance_names "$1"); do
    case "$name" in
      "$1"-*) instance_running "$name" && echo $((9000 + base - 8000)) && return ;;
    esac
  done
}

changed_methods_diff() { # <svc> <worktree> → "changed|empty|nospec <список>"
  local svc="$1" wt="$2" repo="$UPL_HOME/services/$(svc_repo "$svc")"
  if [ "$svc" = "bff" ]; then
    local a="$repo/configs/aggregation.yaml" b="$wt/configs/aggregation.yaml"
    [ -f "$a" ] && [ -f "$b" ] || { echo "nospec"; return 0; }
    local list
    list="$({ diff <(grep -E '^[[:space:]]+(method|path):' "$a") <(grep -E '^[[:space:]]+(method|path):' "$b") \
      | grep '^[<>]' || true; } | paste -sd' ' - | sed 's/[[:space:]]\+/ /g')"
    [ -n "$list" ] && echo "changed $list" || echo "empty"
    return 0
  fi
  local a="$repo/api/docs/swagger.json" b="$wt/api/docs/swagger.json"
  [ -f "$a" ] && [ -f "$b" ] || { echo "nospec"; return 0; }
  local res
  res="$(python3 - "$a" "$b" <<'PY'
import json, sys
def ops(path):
    out = {}
    for p, items in json.load(open(path)).get("paths", {}).items():
        for m, op in items.items():
            if m in ("get", "post", "put", "patch", "delete"):
                out[f"{m.upper()} {p}"] = json.dumps(op, sort_keys=True)
    return out
a, b = ops(sys.argv[1]), ops(sys.argv[2])
changed = []
for k in sorted(set(a) | set(b)):
    if a.get(k) != b.get(k):
        changed.append(k)
print("empty" if not changed else "changed " + " ".join(changed))
PY
)"
  echo "$res"
}

accept_affected() { # <sfx> → затронутые сервисы задачи: worktree, живой экземпляр или task-ветка.
  # Ветка переживает завершение задачи и снятие worktree, поэтому по ней поднимается
  # и приёмка уже завершённой задачи (например, стенды из инвентаризации).
  local svc sfx="$1" affected=""
  for svc in $ALL_SERVICES; do
    if [ -d "$(worktree_path "$svc" "$sfx")" ] || instance_running "$svc-$sfx" \
      || git -C "$UPL_HOME/services/$(svc_repo "$svc")" show-ref --verify -q "refs/heads/feature/UPL-$sfx" 2>/dev/null; then
      affected="$affected $svc"
    fi
  done
  echo "$affected"
}

task_src() { # <svc> <sfx> -> дерево кода задачного экземпляра: worktree или чекаут на ветке задачи
  local wt repo
  wt="$(worktree_path "$1" "$2")"
  if [ -d "$wt" ]; then printf '%s' "$wt"; return 0; fi
  repo="$UPL_HOME/services/$(svc_repo "$1")"
  [ "$(git -C "$repo" branch --show-current 2>/dev/null | tr '[:upper:]' '[:lower:]')" = "feature/upl-$2" ] && printf '%s' "$repo"
}

# Фоновые процессы, чей код меняет задача: изменённый файл лежит в пакете
# сборки cmd/<proc> (go list -deps), либо задача меняет go.mod, go.sum,
# configs/.env.example или несёт свои миграции: процесс задачи работает в её
# БД, а develop-процесс этой БД не видит. Без origin/develop или без списка
# пакетов процесс считается затронутым: лишний запуск безопаснее пропущенного.
task_bg_procs() { # <дерево задачи> -> затронутые фоновые процессы через пробел
  local src="$1" procs p changed mod pkgs f d hit
  procs="$(svc_bg_procs "$src")"
  [ -n "$procs" ] || return 0
  if ! git -C "$src" rev-parse -q --verify origin/develop >/dev/null 2>&1; then
    printf '%s' "$procs"; return 0
  fi
  changed="$( { git -C "$src" diff --name-only --no-renames origin/develop...HEAD
                git -C "$src" diff --name-only --no-renames HEAD
                git -C "$src" ls-files -o --exclude-standard; } 2>/dev/null | sort -u )"
  [ -n "$changed" ] || return 0
  if printf '%s\n' "$changed" | grep -Eq '^(go\.mod|go\.sum|configs/\.env\.example|migrations/)'; then
    printf '%s' "$procs"; return 0
  fi
  mod="$(cd "$src" && GOWORK=off GOPROXY=off go list -m 2>/dev/null)"
  for p in $procs; do
    pkgs="$(cd "$src" && GOWORK=off GOPROXY=off go list -e -deps -f '{{.ImportPath}}' "./cmd/$p" 2>/dev/null)"
    if [ -z "$mod" ] || [ -z "$pkgs" ]; then printf '%s ' "$p"; continue; fi
    hit=0
    while IFS= read -r f; do
      case "$f" in *_test.go) continue ;; esac
      d="$(dirname "$f")"
      if [ "$d" = "." ]; then d="$mod"; else d="$mod/$d"; fi
      if printf '%s\n' "$pkgs" | grep -qxF "$d"; then hit=1; break; fi
    done <<EOF
$changed
EOF
    [ "$hit" = "1" ] && printf '%s ' "$p"
  done
}

cmd_accept() { # <UPL-XXXX>
  local key="$1"; [ -n "${key:-}" ] || die "accept: укажи ключ задачи"
  local sfx; sfx="$(task_suffix "$key")"; [ -n "$sfx" ] || die "accept: не распознал суффикс в '$key'"
  mkdir -p "$RT/env" "$RT/log" "$RT/run"
  cmd_infra up

  local svc affected
  affected="$(accept_affected "$sfx")"
  [ -n "$(echo "$affected" | tr -d ' ')" ] || { info "нет затронутых сервисов задачи $key (нет worktree, экземпляра или task-ветки)"; return 0; }

  for svc in $affected; do
    # Автоцикла нет: поднятый экземпляр задачи гасим, чтобы accept поднял актуальный код.
    # gateway: экземпляра на хосте нет, cmd_svc_up сам рендерит конфиг и держит контейнер.
    instance_running "$svc-$sfx" && stop_instance "$svc-$sfx"
    cmd_svc_up "$svc" --task "$key"
  done
  # Фоновые процессы задачи: те, чей код она меняет (task_bg_procs).
  local p tsrc bg_started=""
  for svc in $affected; do
    [ "$(svc_kind "$svc")" = "go" ] || continue
    tsrc="$(task_src "$svc" "$sfx")"
    [ -n "$tsrc" ] || continue
    for p in $(task_bg_procs "$tsrc"); do
      instance_running "$(instance_name "$svc" "$sfx" "$p")" && stop_instance "$(instance_name "$svc" "$sfx" "$p")"
      cmd_svc_up "$svc" --task "$key" --proc "$p"
      bg_started="$bg_started $(instance_name "$svc" "$sfx" "$p")"
    done
  done
  case " $affected " in
    *" envoy "*) ;; # конфиг задачи уже отрендерен cmd_svc_up envoy
    *) envoy_render_base ;;
  esac
  docker ps --format '{{.Names}}' | grep -qx envoy && sleep 2 # file provider подхватывает конфиг

  # bff-блок задачи обязан иметь правила в отрендеренных маршрутах envoy:
  # отсутствие правила иначе ловится только этим прогоном (environment.md).
  # Сверяются routes[].match (method+path; :param покрыт regex-правилом) задачной
  # aggregation.yaml против origin/develop-базовой; secondary/filter и пути без
  # match не требуют envoy-правил.
  local agg="$UPL_HOME/services/bff-UPL-$sfx/configs/aggregation.yaml"
  if [ -f "$agg" ]; then
    local aggmiss
    aggmiss="$(python3 - "$agg" "$UPL_HOME/services/bff" "$RT/envoy/config/routes.yaml" <<'PY'
import re, subprocess, sys
agg_path, bff_repo, routes_path = sys.argv[1], sys.argv[2], sys.argv[3]

def agg_endpoints(text):
    eps = set()
    for m in re.finditer(r"  - match:\s+method: (\S+)\s+path: (\S+)", text):
        eps.add((m.group(1), m.group(2)))
    for m in re.finditer(r"  - match:\s+path: (\S+)\s+method: (\S+)", text):
        eps.add((m.group(2), m.group(1)))
    return eps

base = subprocess.run(["git", "-C", bff_repo, "show",
                       "origin/develop:configs/aggregation.yaml"],
                      capture_output=True, text=True).stdout
new = agg_endpoints(open(agg_path).read()) - agg_endpoints(base)

bff_matches = []
for doc in open(routes_path).read().split("\n---\n"):
    if "kind: HTTPRoute" not in doc:
        continue
    if re.search(r"kind: Backend\s*\n\s*name: bff", doc) is None:
        continue
    for mm in re.finditer(r"- path:\s+type: (\S+)\s+value: (.+)\s+method: (\S+)", doc):
        bff_matches.append((mm.group(1), mm.group(2).strip(), mm.group(3)))
    for mm in re.finditer(r"- method: (\S+)\s+path:\s+type: (\S+)\s+value: (.+)", doc):
        bff_matches.append((mm.group(2), mm.group(3).strip(), mm.group(1)))

def covered(method, path):
    for ptype, value, pmethod in bff_matches:
        if pmethod != method:
            continue
        if ptype == "Exact" and value == path:
            return True
        if ptype == "RegularExpression":
            v = value.strip("^$")
            if ":" in path:
                if path.startswith(v.split("(")[0]):
                    return True
            elif v == path:
                return True
        if ptype == "PathPrefix" and path.startswith(value):
            return True
    return False

missing = [f"{m} {p}" for m, p in sorted(new) if not covered(m, p)]
print(" ".join(missing))
PY
)"
    [ -z "$aggmiss" ] || die "accept: нет envoy-правил для агрегированных endpoints задачи:$aggmiss — добавь правило в routes.yaml envoy и повтори accept"
  fi

  info ""
  info "Приёмка $key — envoy: http://localhost:8110"
  local svc port methods res
  for svc in $affected; do
    if [ "$(svc_kind "$svc")" = "gateway" ]; then
      info "• $svc: конфиг задачи применён (gateway http://localhost:$(svc_port "$svc"))"
      continue
    fi
    port="$(instance_task_port "$svc")"
    [ -n "$port" ] || port="$(svc_port "$svc")"
    res="$(changed_methods_diff "$svc" "$(worktree_path "$svc" "$sfx")")"
    methods="${res#* }"
    case "$res" in
      empty) ;;
      nospec)
        info "• $svc: http://localhost:$port/swagger/index.html (спека не сгенерирована - изменённые методы сверь вручную)" ;;
      changed*)
        info "• $svc: http://localhost:$port/swagger/index.html - изменённые: $methods" ;;
    esac
  done
  for p in $bg_started; do
    info "• $p: фоновый процесс задачи, порт health/metrics $(proc_port "${p%%.*}" "$(inst_proc "$p")")"
  done
}

# ── task close ───────────────────────────────────────────────────────────────

cmd_task_close() { # <UPL-XXXX>
  local key="$1"; [ -n "${key:-}" ] || die "task close: укажи ключ задачи"
  local sfx; sfx="$(task_suffix "$key")"; [ -n "$sfx" ] || die "task close: не распознал суффикс в '$key'"

  local svc wt repo dbname dbh touched="" p
  for svc in $ALL_SERVICES; do
    wt="$(worktree_path "$svc" "$sfx")"
    repo="$UPL_HOME/services/$(svc_repo "$svc")"
    [ -d "$wt" ] || [ -e "$(pidfile "$svc-$sfx")" ] || compgen -G "$RT/run/$svc.*-$sfx.pid" >/dev/null || continue
    touched="$touched $svc"
    dbh="$(svc_dbhost "$svc" 2>/dev/null || true)"
    dbname="$(svc_dbname "$svc")_$sfx"

    # 1. Миграции задачи — на основную БД сервиса.
    if [ -n "$dbh" ] && [ -d "$wt/cmd/migrate" ]; then
      local want have
      want="$(max_migration "$wt")"
      have="$(migrate_applied_version "$dbh" "$(svc_dbname "$svc")")"
      if [ -n "$want" ] && [ "$((10#${want:-0}))" -gt "$((10#${have:-0}))" ]; then
        local mainenv="$(envfile "$svc")"
        [ -f "$mainenv" ] || { render_env "$svc" "$UPL_HOME/services/$(svc_repo "$svc")" "$mainenv" "$(svc_port "$svc")" "$(svc_dbname "$svc")" 0; }
        run_migrate "$wt" "$mainenv" up
        info "$svc: миграции до $want применены на основную БД $(svc_dbname "$svc")"
      else
        info "$svc: новых миграций для основной БД нет (БД=$have, задача=$want)"
      fi
    fi

    # 2. Останов экземпляров задачи: api и фоновых процессов.
    stop_instance "$svc-$sfx" 2>/dev/null
    for p in $BG_PROCS; do stop_instance "$svc.$p-$sfx" 2>/dev/null; done

    # 3. БД задачи.
    [ -n "$dbh" ] && { drop_db "$dbh" "$dbname" && info "$svc: БД $dbname удалена"; }

    # 4. Worktree и ветка. Грязный worktree не удаляем: незакоммиченный код
    # задачи снимает человек, авто-удаление касается только чистого дерева.
    if [ -d "$wt" ]; then
      if [ -n "$(git -C "$wt" status --porcelain)" ]; then
        info "$svc: worktree $(basename "$wt") ГРЯЗНЫЙ (незакоммиченные файлы) - оставлен; сними вручную после разбора"
      elif git -C "$repo" worktree remove "$wt" 2>/dev/null; then
        info "$svc: worktree $(basename "$wt") снят"
      else
        info "$svc: worktree $(basename "$wt") НЕ снят - сними вручную (git worktree remove)"
      fi
    fi
    local branch="feature/UPL-$sfx"
    if git -C "$repo" rev-parse --verify -q "refs/heads/$branch" >/dev/null; then
      # -d проверяет upstream ветки, а не канон: неслитая ветка удалялась бы
      # молча. Явная проверка ancestry относительно основной ветки репозитория.
      local base
      base="$(svc_base_branch "$svc" 2>/dev/null || echo develop)"
      if git -C "$repo" merge-base --is-ancestor "$branch" "$base" 2>/dev/null; then
        git -C "$repo" branch -d "$branch" 2>/dev/null
        info "$svc: ветка $branch удалена (слита в $base)"
      else
        info "$svc: ветка $branch ОСТАВЛЕНА — не слита в $base; удали вручную после MR"
      fi
    fi
  done

  refresh_peers $touched # A1: задачные экземпляры сняты — пиры на develop-адреса
  envoy_render_base
  docker ps --format '{{.Names}}' | grep -qx envoy && sleep 2
  info "задача $key закрыта"
}

# ── develop postgres (read-only; sync) ───────────────────────────────────────
# Зафиксированная схема (2026-10-07, сессия консолидации стенда):
#   - креды: projects/upl/.data/config.toml, секция [develop_db]; в сессию,
#     логи и args процессов не выводятся (ни пользователь, ни пароль).
#   - host psql - это shim (~/.local/bin/psql, docker exec db psql): он
#     пробрасывает только PGHOST/PGDATABASE/PGPORT/PGUSER/PGPASSWORD/PGSSL*,
#     PGOPTIONS не долетает - read-only включается options-параметром conninfo;
#     guard шима требует PGHOST и PGDATABASE - экспортировать перед вызовом,
#     PGDATABASE = целевая БД (postgres на develop запрещён pg_hba).
#   - pg_dump на хосте нет - клиент из контейнера db (pg 17 = серверу), явные
#     флаги --host/--port/--username/--dbname (conninfo в argv разваливается),
#     PGPASSWORD/PGOPTIONS - через docker exec -e.
#     В pg_dump read-only включается -e PGOPTIONS (контейнеру шим не мешает).
#   - миграции всегда из origin/develop (detached worktree во временном
#     каталоге), не из чекаутов: их ветки бывают задачными.
#   - dev sync: (1) БД + проверка подключения read-only; (2) миграции из
#     origin/develop; (3) структурная сверка схем; (4) схема разошлась -
#     human-gate (клонировать полным дампом / пропустить); строки разошлись
#     хотя бы в одной таблице - перелив данных всей БД; иначе БД не трогается.

DEV_CONF="$(cd "$SCRIPT_DIR/.." && pwd)/.data/config.toml"
DEV_DBS="auth user catalog ord report tracking"
DEV_MIGRATE_DIR="$RT/develop-migrate"

dev_env() { # → DEV_HOST/DEV_PORT/DEV_USER/DEV_PASSWORD (eval; без вывода значений)
  [ -f "$DEV_CONF" ] || die "нет $DEV_CONF - заполни [develop_db] по рыбе develop-db.env.example"
  python3 - "$DEV_CONF" <<'PY'
import re, sys
t = open(sys.argv[1]).read()
m = re.search(r'\[develop_db\]([^\[]*)', t)
d = dict(re.findall(r'(\w+)\s*=\s*"?([^"\n]+)"?', m.group(1))) if m else {}
for k in ('host', 'port', 'user', 'password'):
    v = d.get(k, '').strip()
    if v:
        print('DEV_%s="%s"' % (k.upper(), v))
    elif k != 'port':
        print("die 'в [develop_db] не заполнено: %s'" % k)
PY
}

dev_psql() { # <db> <sql> → результат; read-only через conninfo, PGDATABASE = <db>
  [ -n "${DEV_HOST:-}" ] || die "dev_psql: креды не загружены"
  [ -n "${1:-}" ] || die "dev_psql: не указана БД"
  PGHOST="$DEV_HOST" PGPORT="${DEV_PORT:-5432}" PGUSER="$DEV_USER" PGPASSWORD="$DEV_PASSWORD" \
  PGDATABASE="$1" psql "host=$DEV_HOST port=${DEV_PORT:-5432} user=$DEV_USER dbname=$1 options='-c default_transaction_read_only=on'" -tAc "$2"
}

dev_pg_dump() { # <db> <флаги pg_dump...> - клиент из контейнера db; дамп в stdout хоста
  local db="$1"; shift
  [ -n "$db" ] || die "dev_pg_dump: не указана БД"
  docker exec -e PGPASSWORD="$DEV_PASSWORD" \
    -e PGOPTIONS='-c default_transaction_read_only=on' \
    db pg_dump --host "$DEV_HOST" --port "${DEV_PORT:-5432}" --username "$DEV_USER" \
    --dbname "$db" --no-owner --no-privileges "$@"
}

dev_schema_dump() { # <db> dev|local → stdout; CHECK-строки нормализованы
  # Нормализация CHECK: deparse legacy-деревьев на develop отличается от
  # канонического pg17 (группировка ARRAY/касты) - сравниваем значения и
  # колонки, скобки/касты/скобки-массивы стираем только в этих строках.
  # sed BSD не понимает класс [][()] - поэтому отдельные подстановки.
  if [ "$2" = "dev" ]; then
    dev_pg_dump "$1" --schema-only --no-privileges \
      | grep -v -e '^-- Dumped from' -e '^\\restrict' -e '^\\unrestrict' \
      | sed -E -e '/CONSTRAINT .*CHECK/s/\(/ /g' -e '/CONSTRAINT .*CHECK/s/\)/ /g' \
              -e '/CONSTRAINT .*CHECK/s/\[/ /g' -e '/CONSTRAINT .*CHECK/s/\]/ /g' \
              -e '/CONSTRAINT .*CHECK/s/::character varying//g' -e '/CONSTRAINT .*CHECK/s/::text//g' \
              -e '/CONSTRAINT .*CHECK/s/ ,/,/g' -e '/CONSTRAINT .*CHECK/s/ +/ /g'
  else
    docker exec db pg_dump --schema-only --no-owner --no-privileges -U postgres -d "$1" \
      | grep -v -e '^-- Dumped from' -e '^\\restrict' -e '^\\unrestrict' \
      | sed -E -e '/CONSTRAINT .*CHECK/s/\(/ /g' -e '/CONSTRAINT .*CHECK/s/\)/ /g' \
              -e '/CONSTRAINT .*CHECK/s/\[/ /g' -e '/CONSTRAINT .*CHECK/s/\]/ /g' \
              -e '/CONSTRAINT .*CHECK/s/::character varying//g' -e '/CONSTRAINT .*CHECK/s/::text//g' \
              -e '/CONSTRAINT .*CHECK/s/ ,/,/g' -e '/CONSTRAINT .*CHECK/s/ +/ /g'
  fi
}

dev_rows() { # <db> dev|local → суммарное число строк public
  local q
  if [ "$2" = "dev" ]; then
    q="$(dev_psql "$1" "SELECT coalesce(string_agg(format('SELECT %L AS t, count(*) AS n FROM %I.%I', tablename, schemaname, tablename), ' UNION ALL '),'SELECT 0 AS t, 0 AS n') FROM pg_tables WHERE schemaname='public'")"
    dev_psql "$1" "$q" | awk -F'|' '{s+=$2} END{print s+0}'
  else
    q="$(docker exec db psql -X -tA -U postgres -d "$1" -c "SELECT coalesce(string_agg(format('SELECT %L AS t, count(*) AS n FROM %I.%I', tablename, schemaname, tablename), ' UNION ALL '),'SELECT 0 AS t, 0 AS n') FROM pg_tables WHERE schemaname='public'")"
    docker exec db psql -X -tA -U postgres -d "$1" -c "$q" | awk -F'|' '{s+=$2} END{print s+0}'
  fi
}

dev_ensure_db() { # <db> - шаг 1: БД существует или создаётся
  local n
  n="$(docker exec db psql -X -tA -U postgres -d postgres -c "SELECT 1 FROM pg_database WHERE datname='$1'" 2>/dev/null | tr -d '[:space:]')"
  [ "$n" = "1" ] && return 0
  docker exec db psql -X -q -U postgres -d postgres -c "CREATE DATABASE \"$1\"" || die "не создана БД $1"
  info "шаг 1: БД $1 создана"
}

dev_migrate() { # шаг 2: миграции сервиса из origin/develop (detached worktree)
  local svc="$1" db="$2"
  local repo="$UPL_HOME/services/$(svc_repo "$svc")"
  git -C "$repo" rev-parse -q --verify origin/develop >/dev/null || die "в $(svc_repo "$svc") нет origin/develop"
  local wt="$DEV_MIGRATE_DIR/$(svc_repo "$svc")"
  mkdir -p "$DEV_MIGRATE_DIR"
  [ -d "$wt" ] || git -C "$repo" worktree add --detach "$wt" origin/develop >/dev/null \
    || die "worktree миграций для $(svc_repo "$svc") не создан"
  copy_local_env "$repo" "$wt" # все создаваемые worktree получают локальные .env
  local envf="$DEV_MIGRATE_DIR/$svc.env"
  cp "$wt/configs/.env.example" "$envf"
  env_set "$envf" DB_HOST localhost
  env_set "$envf" DB_PORT 5432
  env_set "$envf" DB_NAME "$db"
  ( cd "$wt" && CONFIG_PATH="$envf" go run ./cmd/migrate -direction up >/dev/null ) \
    || die "миграции $svc ($db) из origin/develop упали"
  local v m
  v="$(docker exec db psql -X -tA -U postgres -d "$db" -c 'SELECT version FROM schema_migrations' 2>/dev/null | tr -d '[:space:]')"
  m="$(ls "$wt/migrations"/*.up.sql 2>/dev/null | sed 's!.*/!!;s/_.*//' | sort -n | tail -1)"
  info "шаг 2: $db = $v (origin/develop max $m)"
  git -C "$repo" worktree remove --force "$wt" >/dev/null 2>&1
  rm -f "$envf"
}

dev_schema_match() { # <db> → 0 если схемы идентичны
  local a="$RT/.schema-dev-$1.sql" b="$RT/.schema-local-$1.sql"
  dev_schema_dump "$1" dev > "$a"
  dev_schema_dump "$1" local > "$b"
  diff -q "$a" "$b" >/dev/null 2>&1
  local rc=$?
  rm -f "$a" "$b"
  return $rc
}

cmd_dev_sync() { # шаги 1-4: БД+read-only → миграции из develop → сверка схем → human-gate/перелив
  eval "$(dev_env)"
  local d svc db ro
  info "шаг 1: БД + проверка подключения к develop (read-only)"
  ro="$(dev_psql auth "select current_setting('default_transaction_read_only')" 2>&1)" \
    || die "develop недоступен: $ro"
  [ "$ro" = "on" ] || die "подключение к develop НЕ в read-only (default_transaction_read_only=$ro) - по правилу продолжение запрещено"
  info "шаг 1: подключение read-only подтверждено"
  for d in $DEV_DBS; do dev_ensure_db "$d"; done
  info "шаг 2: миграции из origin/develop"
  for svc in auth user catalog ord report tracking; do
    db="$(svc_dbname "$svc")"
    dev_migrate "$svc" "$db"
  done
  info "шаг 3: сверка схем (версии миграций → детальная)"
  local mismatch="" dv lv
  for d in $DEV_DBS; do
    dv="$(dev_psql "$d" 'select version from schema_migrations' 2>/dev/null | tr -d '[:space:]')"
    lv="$(docker exec db psql -X -tA -U postgres -d "$d" -c 'select version from schema_migrations' 2>/dev/null | tr -d '[:space:]')"
    if [ "$((10#${dv:-0}))" != "$((10#${lv:-0}))" ]; then
      die "шаг 3: $d - версии миграций расходятся (develop=${dv:-?} local=${lv:-?}) - стоп, разбор вручную"
    fi
    if dev_schema_match "$d"; then
      info "шаг 3: $d - версия $lv, схема совпадает"
    else
      info "шаг 3: $d - версия $lv, схема РАСХОДИТСЯ"
      mismatch="$mismatch $d"
    fi
  done
  info "шаг 4: сверка строк; схема не совпала - human-gate"
  local report="" before_f after_f diff_tables action changed a
  for d in $DEV_DBS; do
    before_f="$RT/.rows-before-$d"; after_f="$RT/.rows-after-$d"
    dev_table_counts "$d" local | sort > "$before_f"
    action="не обновлялась"
    case " $mismatch " in
      *" $d "*)
        printf 'шаг 4: схема %s расходится с develop. [к]лонировать полным дампом с develop / [п]ропустить? ' "$d"
        a=""
        read -r a || a=""
        case "$a" in
          к*|К*)
            action="обновлена (клонирование: схема разошлась)"
            info "шаг 4: $d - клонирование полным дампом"
            docker exec db psql -X -q -U postgres -d postgres -c "DROP DATABASE IF EXISTS \"$d\" WITH (FORCE)" >/dev/null
            docker exec db psql -X -q -U postgres -d postgres -c "CREATE DATABASE \"$d\"" >/dev/null
            dev_pg_dump "$d" | docker exec -i \
              -e PGPASSWORD="$DEV_PASSWORD" -e PGOPTIONS='-c session_replication_role=replica' \
              db psql -X -q -1 -v ON_ERROR_STOP=1 -U postgres -d "$d" -f - >/dev/null \
              || die "restore $d из полного дампа не прошёл"
            info "шаг 4: $d пересобрана" ;;
          *)
            action="не обновлялась (схема разошлась, решение человека)"
            info "шаг 4: $d - пропущено по решению человека" ;;
        esac
        dev_table_counts "$d" local | sort > "$after_f"
        changed="$(awk -F'|' 'NR==FNR{b[$1]=$2; next} b[$1]!=$2{printf "%s: %s → %s; ", $1, b[$1]+0, $2+0}' "$before_f" "$after_f")"
        if [ -n "$changed" ]; then
          report="$report
$d: $action; изменившиеся таблицы: ${changed%; }"
        else
          report="$report
$d: $action; изменившихся таблиц нет"
        fi
        rm -f "$before_f" "$after_f"
        continue ;;
    esac
    # схема совпадает: перелив данных только при несовпадении строк хотя бы одной таблицы
    # (develop против локального снапшота, а не локальное с локальным)
    local diff_tables
    diff_tables="$(comm -3 <(dev_table_counts "$d" dev | sort) "$before_f" | awk -F'|' '{gsub(/[ \t]/, "", $1); print $1}' | sort -u | tr '\n' ' ')"
    if [ -n "$(echo $diff_tables)" ]; then
      action="обновлена (перелив данных)"
      info "шаг 4: $d - расхождение строк в таблицах:$diff_tables - перелив данных"
      local list
      list="$(docker exec db psql -X -tA -U postgres -d "$d" -c "SELECT string_agg(format('%I.%I', schemaname, tablename), ', ') FROM pg_tables WHERE schemaname='public' AND tablename <> 'schema_migrations'")"
      [ -n "$list" ] && docker exec db psql -X -q -U postgres -d "$d" -c "TRUNCATE $list CASCADE"
      dev_pg_dump "$d" --data-only --exclude-table=schema_migrations | docker exec -i \
        -e PGPASSWORD="$DEV_PASSWORD" -e PGOPTIONS='-c session_replication_role=replica' \
        db psql -X -q -1 -v ON_ERROR_STOP=1 -U postgres -d "$d" -f - >/dev/null \
        || die "перелив данных $d не прошёл"
      info "шаг 4: $d - данные перелиты"
    fi
    dev_table_counts "$d" local | sort > "$after_f"
    changed="$(awk -F'|' 'NR==FNR{b[$1]=$2; next} b[$1]!=$2{printf "%s: %s → %s; ", $1, b[$1]+0, $2+0}' "$before_f" "$after_f")"
    if [ -n "$changed" ]; then
      report="$report
$d: $action; изменившиеся таблицы: ${changed%; }"
    else
      report="$report
$d: $action; изменившихся таблиц нет"
    fi
    rm -f "$before_f" "$after_f"
  done
  info "итог по БД:"
  info "$report"
  cmd_dev_check
}

dev_table_counts() { # <db> dev|local → строки "таблица|число" (только public)
  local q
  if [ "$2" = "dev" ]; then
    q="$(dev_psql "$1" "SELECT coalesce(string_agg(format('SELECT %L AS t, count(*) AS n FROM %I.%I', tablename, schemaname, tablename), ' UNION ALL '),'SELECT 0 AS t, 0 AS n') FROM pg_tables WHERE schemaname='public'")"
    dev_psql "$1" "$q"
  else
    q="$(docker exec db psql -X -tA -U postgres -d "$1" -c "SELECT coalesce(string_agg(format('SELECT %L AS t, count(*) AS n FROM %I.%I', tablename, schemaname, tablename), ' UNION ALL '),'SELECT 0 AS t, 0 AS n') FROM pg_tables WHERE schemaname='public'")"
    docker exec db psql -X -tA -U postgres -d "$1" -c "$q"
  fi
}

cmd_dev_check() { # отчёт: версии, строки, структурная сверка схем
  eval "$(dev_env)"
  local d rd rl
  printf '%-10s %-8s %-8s %-9s %-9s %s\n' "DB" "SCHEMA(dev)" "SCHEMA(loc)" "ROWS(dev)" "ROWS(loc)" "ИТОГ"
  for d in $DEV_DBS; do
    local r="$(dev_psql "$d" 'select version from schema_migrations' 2>/dev/null | tr -d '[:space:]')"
    local l="$(docker exec db psql -X -tA -U postgres -d "$d" -c 'select version from schema_migrations' 2>/dev/null | tr -d '[:space:]')"
    rd="$(dev_rows "$d" dev)"; rl="$(dev_rows "$d" local)"
    local ok=OK
    [ "$((10#${r:-0}))" = "$((10#${l:-0}))" ] && dev_schema_match "$d" || ok=РАСХОЖДЕНИЕ
    printf '%-10s %-8s %-8s %-9s %-9s %s\n' "$d" "${r:-?}" "${l:-?}" "$rd" "$rl" "$ok (строки: живой develop, расхождение счётчиков может быть benign)"
  done
}

# ── seed / doctor ────────────────────────────────────────────────────────────

cmd_seed() {
  local svc="${1:-catalog}"
  case "$svc" in
    catalog)
      local sql="$UPL_HOME/services/catalog-db-smoke.sql"
      [ -f "$sql" ] || die "$sql не найден"
      docker exec -i db psql -X -q -1 -v ON_ERROR_STOP=1 -v smoke_count=5 -U postgres -d catalog -f - < "$sql" \
        || die "засев catalog не прошёл"
      info "catalog засеян smoke-данными (db)"
      ;;
    *) die "seed: поддержан только catalog" ;;
  esac
}

cmd_doctor() {
  local ok=1 msg
  command -v docker >/dev/null && command -v go >/dev/null || { info "нет docker или go в PATH"; ok=0; }
  [ -d "$UPL_HOME/services" ] || { info "$UPL_HOME/services не найден (задай UPL_HOME)"; ok=0; }
  local p who
  for p in 5432 5433 6379 9092 9094 5672 15672 9000 9001 16686 4317 8110; do
    if port_busy "$p"; then
      who="$(lsof -nP -iTCP:"$p" -sTCP:LISTEN | awk 'NR==2{print $1"("$2")"}')"
      msg="$msg порт $p занят ($who);"
    fi
  done
  [ -n "$msg" ] && { info "конфликты портов:$msg"; ok=0; }
  [ "$ok" = "1" ] && info "doctor: всё чисто" || info "doctor: есть замечания"
}

# ── Диспетчер ────────────────────────────────────────────────────────────────

case "${1:-}" in
  infra) shift; cmd_infra "$@" ;;
  svc)
    shift
    action="${1:-}"; shift || true
    case "$action" in
      up|down|logs|migrate)
        s="${1:-}"; shift || true
        case "$action" in
          up) if [ -z "$s" ]; then cmd_svc_up_all
              elif [ "$s" = "--task" ]; then
                t="${1:-}"; [ -n "$t" ] || die "svc up: --task требует ключ, например UPL-1226"
                cmd_svc_up_branch "feature/UPL-$(task_suffix "$t")"
              elif svc_repo "$s" >/dev/null 2>&1; then cmd_svc_up "$s" "$@"
              else die "svc up: неизвестный сервис '$s' (задачную ветку на все сервисы: svc up --task UPL-XXXX)"; fi ;;
          down) if [ -z "$s" ]; then cmd_svc_down_all; else cmd_svc_down "$s" "$@"; fi ;;
          logs) cmd_svc_logs "$s" "$@" ;;
          migrate) cmd_svc_migrate "$s" "$@" ;;
        esac ;;
      status) cmd_svc_status ;;
      switch) cmd_svc_switch "$@" ;;
      *) die "svc: неизвестное действие '$action'" ;;
    esac ;;
  seed) shift; cmd_seed "$@" ;;
  accept) shift; cmd_accept "$@" ;;
  task)
    shift
    sub="${1:-}"; shift || true
    case "$sub" in
      close) cmd_task_close "$@" ;;
      *) die "task: неизвестное действие '$sub'" ;;
    esac ;;
  doctor) cmd_doctor ;;
  dev)
    shift
    sub="${1:-}"; shift || true
    case "$sub" in
      sync) cmd_dev_sync ;;
      check) cmd_dev_check ;;
      *) die "dev: неизвестное действие '$sub' (sync|check)" ;;
    esac ;;
  __supervisor) shift; cmd_supervisor "$@" ;;
  ""|-h|--help|help)
    awk 'NR>1 && /^#/{sub(/^# ?/,""); print; next} NR>1{exit}' "$0" ;;
  *) die "неизвестная команда '$1' (см. $0 без аргументов)" ;;
esac
