#!/usr/bin/env bash
# Регресс-тест upl-infra.sh svc switch / gateway-путей (envoy в реестре).
# Автономный: строит свой sandbox в /tmp (фейковые git-репозитории), сеть и
# docker не нужны. Прогон ~5 c. Запуск: bash test_upl_infra_switch.sh
#
# Покрывает регрессии:
#   - основная ветка репозитория: develop, если есть, иначе канон (master/main);
#     запросы develop|master|main ведут на одну и ту же основную ветку
#   - чекаут gateway (envoy) не переключается на задачную ветку: конфиг из worktree
#   - грязный чекаут блокирует смену ветки отказом всей команды
#   - svc up envoy / svc up envoy --task рендерят конфиг из чекаута/worktree
#   - refresh_peers: сменившийся адрес пира перезапускает разв-экземпляр
#     и уже поднятый задачный экземпляр
set -u
SCR="$(cd "$(dirname "$0")" && pwd)/upl-infra.sh"
SB="$(mktemp -d "${TMPDIR:-/tmp}/upl-infra-test.XXXXXX")"
FAILURES=0; RC=0; OUT=""
trap 'rm -rf "$SB"; kill "$(cat "$SB/.infra/run/auth.pid" 2>/dev/null)" 2>/dev/null' EXIT

ck() { local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then echo "ok: $desc"; else echo "FAIL: $desc"; FAILURES=$((FAILURES+1)); fi
}
run() { OUT=$(UPL_HOME=$SB bash "$SCR" "$@" 2>&1); RC=$?; printf '%s\n' "$OUT" | sed 's/^/   /'; }
alive() { kill -0 "$1" 2>/dev/null; }
dead() { ! kill -0 "$1" 2>/dev/null; }
noref() { ! git -C "$1" show-ref --verify -q "refs/heads/$2"; }
FAKEPID=""
trap '[ -n "$FAKEPID" ] && alive "$FAKEPID" && kill "$FAKEPID" 2>/dev/null; rm -rf "$SB"' EXIT
BR() { git -C "$1" branch --show-current; }

mkgit() { git init -q -b "$2" "$1"; git -C "$1" config user.email t@t; git -C "$1" config user.name t; }

# ── Фикстура: auth-service (develop), user-service (+feature/UPL-999), envoy
# (master + main + feature/UPL-999; маркеры в backends.yaml различают источник
# рендера: name: auth | auth-master | auth-main | auth-999).
mkgit "$SB/services/auth-service" develop
mkdir -p "$SB/services/auth-service/configs"
printf 'HTTP_PORT=8000\n' > "$SB/services/auth-service/configs/.env.example"
git -C "$SB/services/auth-service" add -A; git -C "$SB/services/auth-service" commit -qm init

mkgit "$SB/services/user-service" develop
git -C "$SB/services/user-service" commit -qm init --allow-empty
git -C "$SB/services/user-service" switch -c feature/UPL-999
git -C "$SB/services/user-service" commit -qm task --allow-empty
git -C "$SB/services/user-service" switch develop

mkgit "$SB/services/envoy" master
mkdir -p "$SB/services/envoy/config"
cat > "$SB/services/envoy/config/backends.yaml" <<'EOF'
---
kind: Backend
metadata:
  name: auth
spec:
  endpoints:
    - ip:
        address: 127.0.0.1
      port: 8100
EOF
printf 'kind: HTTPRoute\nmetadata:\n  name: bff\n' > "$SB/services/envoy/config/routes.yaml"
printf 'admin:\n  access_log:\n      host: {}\n' > "$SB/services/envoy/standalone.yaml"
git -C "$SB/services/envoy" add -A; git -C "$SB/services/envoy" commit -qm base
git -C "$SB/services/envoy" branch develop
git -C "$SB/services/envoy" branch main
sed -i '' 's/name: auth$/name: auth-master/' "$SB/services/envoy/config/backends.yaml"
git -C "$SB/services/envoy" commit -qam marker-master
git -C "$SB/services/envoy" switch -q main
sed -i '' 's/name: auth-master/name: auth-main/' "$SB/services/envoy/config/backends.yaml"
git -C "$SB/services/envoy" commit -qam marker-main
git -C "$SB/services/envoy" switch -q master
git -C "$SB/services/envoy" switch -c feature/UPL-999
sed -i '' 's/name: auth-master/name: auth-999/' "$SB/services/envoy/config/backends.yaml"
git -C "$SB/services/envoy" commit -qam marker-999
git -C "$SB/services/envoy" switch -q master

RT="$SB/.infra"

echo "1. svc switch: основная ветка develop у всех, включая envoy с develop"
run svc switch
ck "auth на develop" [ "$(BR "$SB/services/auth-service")" = develop ]
ck "user на develop" [ "$(BR "$SB/services/user-service")" = develop ]
ck "envoy на develop" [ "$(BR "$SB/services/envoy")" = develop ]
ck "рендер из develop" grep -q "name: auth$" "$RT/envoy/config/backends.yaml"
ck "backends на host.docker.internal" grep -q "host.docker.internal" "$RT/envoy/config/backends.yaml"

echo "2. svc switch main|master: develop в репо — master/main не используются"
run svc switch main
ck "envoy остался на develop (main)" [ "$(BR "$SB/services/envoy")" = develop ]
run svc switch master
ck "envoy остался на develop (master)" [ "$(BR "$SB/services/envoy")" = develop ]

echo "3. без develop канон = master, все базовые запросы ведут на него"
git -C "$SB/services/envoy" switch -q master
git -C "$SB/services/envoy" branch -D develop
run svc switch develop
ck "envoy на master (канон вместо develop)" [ "$(BR "$SB/services/envoy")" = master ]
ck "рендер из master" grep -q "name: auth-master" "$RT/envoy/config/backends.yaml"
run svc switch main
ck "envoy на master (main не используется)" [ "$(BR "$SB/services/envoy")" = master ]
run svc switch
ck "envoy на master (без аргумента)" [ "$(BR "$SB/services/envoy")" = master ]

echo "4. задачная ветка: envoy-чекаут не трогается, конфиг из worktree"
run svc switch feature/UPL-999
ck "envoy-чекаут остался на master" [ "$(BR "$SB/services/envoy")" = master ]
ck "worktree envoy-UPL-999 создан" [ -d "$SB/services/envoy-UPL-999/config" ]
ck "рендер из worktree задачи" grep -q "name: auth-999" "$RT/envoy/config/backends.yaml"
ck "user переключён на задачу" [ "$(BR "$SB/services/user-service")" = feature/UPL-999 ]
ck "auth на develop (ветки нет — fallback)" [ "$(BR "$SB/services/auth-service")" = develop ]

echo "5. грязный чекаут: отказ всей команды, сервис остаётся на задачной ветке"
printf dirt > "$SB/services/user-service/DIRTY"
run svc switch
ck "switch упал" [ $RC -ne 0 ]
ck "user не переключён" [ "$(BR "$SB/services/user-service")" = feature/UPL-999 ]
rm "$SB/services/user-service/DIRTY"

echo "6. svc up envoy: рендер из чекаута (канон); docker заглушен — реальный не трогается"
mkdir -p "$SB/bin"
printf '#!/usr/bin/env bash\n[ "$1" = ps ] && { printf "db\\nenvoy\\n"; exit 0; }\nexit 0\n' > "$SB/bin/docker"
chmod +x "$SB/bin/docker"
OLDPATH=$PATH; PATH="$SB/bin:$PATH"
run svc up envoy
ck "up envoy rc0" [ $RC -eq 0 ]
ck "рендер base" grep -q "name: auth-master" "$RT/envoy/config/backends.yaml"

echo "7. svc up envoy --task: рендер из worktree задачи"
run svc up envoy --task UPL-999
PATH=$OLDPATH
ck "рендер задачи" grep -q "name: auth-999" "$RT/envoy/config/backends.yaml"

echo "7b. локальный master и только origin/develop: основная ветка - develop"
sed '/^case "${1:-}" in/,$d' "$SCR" > "$SB/runner.sh"
git init --bare -q "$SB/origin-envoy.git"
git -C "$SB/services/envoy" remote add origin "$SB/origin-envoy.git"
BASESHA="$(git -C "$SB/services/envoy" rev-parse master~1)"
git -C "$SB/services/envoy" push -q "$SB/origin-envoy.git" "$BASESHA:refs/heads/develop"
git -C "$SB/services/envoy" fetch -q origin # push по URL не создаёт refs/remotes/origin/*
BASE_OUT=$(UPL_HOME=$SB bash -c "source '$SB/runner.sh'; svc_base_branch envoy" 2>&1)
ck "svc_base_branch вернул develop, а не локальный master" [ "$BASE_OUT" = "develop" ]
run svc switch develop
ck "envoy на develop (DWIM из origin)" [ "$(BR "$SB/services/envoy")" = develop ]
ck "рендер из develop-контента" grep -q "name: auth$" "$RT/envoy/config/backends.yaml"

echo "7c. task close: гейт ancestry по канону репозитория (envoy - master)"
git -C "$SB/services/envoy" switch -q master
git -C "$SB/services/envoy" branch -D develop
git -C "$SB/services/envoy" remote remove origin
git -C "$SB/services/envoy" switch -c feature/UPL-888
git -C "$SB/services/envoy" commit -qm t --allow-empty
git -C "$SB/services/envoy" switch -q master
git -C "$SB/services/envoy" merge --no-ff -qm merge-888 feature/UPL-888
git -C "$SB/services/envoy" worktree add -q "$SB/services/envoy-UPL-888" feature/UPL-888
run task close UPL-888
ck "worktree 888 снят"   [ ! -d "$SB/services/envoy-UPL-888" ]
ck "слитая в master ветка удалена" noref "$SB/services/envoy" feature/UPL-888
ck "гейт назвал канон master" bash -c 'printf "%s" "$0" | grep -q "слита в master"' "$OUT"
run task close UPL-999
ck "worktree 999 снят"   [ ! -d "$SB/services/envoy-UPL-999" ]
ck "неслитая в master ветка оставлена" git -C "$SB/services/envoy" show-ref --verify -q "refs/heads/feature/UPL-999"
ck "отказ назвал канон master" bash -c 'printf "%s" "$0" | grep -q "не слита в master"' "$OUT"

echo "8. refresh_peers: сменившийся пир → рестарт разв-экземпляра"
mkdir -p "$RT/run" "$RT/env" "$RT/log"
printf 'USER_SERVICE_BASE_URL=http://localhost:9101\nHTTP_PORT=8100\n' > "$RT/env/auth.env"
bash -c 'exec -a "__supervisor auth fake" sleep 60' & FAKEPID=$!
echo $FAKEPID > "$RT/run/auth.pid"
sed '/^case "${1:-}" in/,$d' "$SCR" > "$SB/runner.sh"
OUT=$(UPL_HOME=$SB bash -c "source '$SB/runner.sh'; refresh_peers user" 2>&1)
ck "устаревший экземпляр остановлен (рестарт предпринят)" dead "$FAKEPID"
ck "pidfile снят" [ ! -e "$RT/run/auth.pid" ]
FAKEPID=""

echo "8b. refresh_peers: подъём задачного пира → рестарт живого задачного экземпляра"
# Регрессия STAND-TASK-PEER-ENV: auth-999 поднят с адресом develop-user (8101),
# появляется user-999 (live 9101) — env auth-999 должен перерендериться (рестарт).
bash -c 'exec -a "__supervisor user-999 fake" sleep 60' & USER999PID=$!
echo $USER999PID > "$RT/run/user-999.pid"
printf 'USER_SERVICE_BASE_URL=http://localhost:8101\nHTTP_PORT=9100\n' > "$RT/env/auth-999.env"
bash -c 'exec -a "__supervisor auth-999 fake" sleep 60' & FAKEPID=$!
echo $FAKEPID > "$RT/run/auth-999.pid"
OUT=$(UPL_HOME=$SB bash -c "source '$SB/runner.sh'; refresh_peers user" 2>&1)
ck "задачный экземпляр с устаревшим пиром остановлен (рестарт предпринят)" dead "$FAKEPID"
ck "pidfile задачного снят" [ ! -e "$RT/run/auth-999.pid" ]
FAKEPID=""
# Обратная ветка: env уже содержит живой адрес пира 9101 — рестарта нет
bash -c 'exec -a "__supervisor auth-999 fake" sleep 60' & FAKEPID=$!
echo $FAKEPID > "$RT/run/auth-999.pid"
printf 'USER_SERVICE_BASE_URL=http://localhost:9101\nHTTP_PORT=9100\n' > "$RT/env/auth-999.env"
OUT=$(UPL_HOME=$SB bash -c "source '$SB/runner.sh'; refresh_peers user" 2>&1)
ck "свежий env задачного не перезапускается" alive "$FAKEPID"
FAKEPID=""
kill "$USER999PID" 2>/dev/null; rm -f "$RT/run/user-999.pid"

echo "9. svc status содержит gateway-строку"
UPL_HOME=$SB bash "$SCR" svc status 2>&1 | grep -q "^envoy" \
  && echo "ok: envoy в status" || { echo "FAIL: envoy не в status"; FAILURES=$((FAILURES+1)); }

echo
if [ "$FAILURES" = 0 ]; then echo "ALL PASS"; else echo "FAILED: $FAILURES"; exit 1; fi
