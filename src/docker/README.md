# Окружение запуска задач omp (docker)

Каталог держит образ и compose-файл окружения, в котором omp выполняет задачи внутри контейнера на сервере. Репозитории и конфигурация провайдеров в образ не входят — они приезжают bind-mount'ом с хоста.

Состав:

- `Dockerfile` — alpine + git, python3, ripgrep, jq, curl, less и релизный бинарник omp с закреплёнными sha256 (arm64/amd64);
- `compose.yaml` — сервис `runner` с монтированиями и переменными из `.env`;
- `example.env` — шаблон `.env` (сам `.env` в git не попадает, см. `../.gitignore`).

## Модель безопасности

- Remote-транспорт git внутри контейнера запрещён системным gitconfig образа (`protocol.allow never`): `git pull/push/fetch/clone` по сети из контейнера не работают, локальные операции (ветки, коммиты, worktree) — работают.
  Синхронизацию с origin выполняет хост.
- Учётные данные и конфигурация omp живут на хосте в отдельном каталоге (`OMP_CONFIG_HOST_DIR`) и монтируются в `/home/agent/.omp` контейнера; `~/.omp` станции в контейнер не попадает.
- `container_name` в compose не задаётся: фиксированное имя пересоздаёт одноимённый чужой контейнер. Проект именуется флагом `-p <имя>`.

## Подготовка на сервере (однократно)

1. Создай каталог репозиториев и склонируй в него нужные репозитории (клоны и pull/push — только на хосте):

       install -d /srv/omp/repos /srv/omp/omp-config
       git -C /srv/omp/repos clone <url>

2. Положи в `/srv/omp/omp-config` конфигурацию провайдеров omp — те файлы, которые на станции живут в `~/.omp`.
3. Скопируй шаблон и поправь под раскладку сервера:

       cd <checkout этого репозитория>/src/docker
       cp example.env .env
       # REPOS_HOST_DIR, OMP_CONFIG_HOST_DIR — пути из шагов 1-2;
       # HOST_UID/HOST_GID — uid:gid владельца файлов на хосте (id -u; id -g),
       # на macOS их игнорирует virtiofs.

## Запуск

Сборка и подъём (контейнер живёт под `sleep infinity`, сессии заходят exec):

    docker compose -f src/docker/compose.yaml -p omp up -d --build

Проверка, что окружение поднялось:

    docker compose -f src/docker/compose.yaml -p omp exec runner omp --version
    docker compose -f src/docker/compose.yaml -p omp exec runner \
        git config --system protocol.allow   # ожидание: never

Интерактивная сессия omp в репозитории:

    docker compose -f src/docker/compose.yaml -p omp exec -it runner omp \
        --cwd /workspace/repos/<repo>

Разовая команда без поднятия сервиса:

    docker compose -f src/docker/compose.yaml -p omp run --rm runner <cmd>

Со станции без ssh на сервер — через docker context:

    docker context create omp-server --docker "ssh://user@server"
    docker --context omp-server compose -f src/docker/compose.yaml -p omp up -d --build
    docker --context omp-server compose -f src/docker/compose.yaml -p omp \
        exec -it runner omp --cwd /workspace/repos/<repo>

## Синхронизация репозиториев

Pull и push выполняет хост в `REPOS_HOST_DIR` — контейнер про сетевой git не знает:

    git -C /srv/omp/repos/<repo> pull
    # работа omp внутри контейнера
    git -C /srv/omp/repos/<repo> push

## Обновление omp до нового релиза

1. Возьми чексуммы из `SHA256SUMS.txt` релиза `https://github.com/can1357/oh-my-pi/releases/download/v<версия>/SHA256SUMS.txt` (`omp-linux-musl-arm64` и `omp-linux-musl-x64`).
2. Обнови `OMP_VERSION`, `OMP_SHA256_ARM64`, `OMP_SHA256_AMD64` в `Dockerfile`.
3. Пересобери и перезапусти: `docker compose ... up -d --build`. Несовпадение чексуммы или неисполнимость бинарника роняет сборку, а не контейнер.

## Переменные `.env`

| Переменная | Назначение | Пример |
|---|---|---|
| `REPOS_HOST_DIR` | Каталог git-репозиториев хоста, виден контейнеру как `/workspace/repos` | `/srv/omp/repos` |
| `OMP_CONFIG_HOST_DIR` | Каталог конфигов провайдеров omp, виден как `/home/agent/.omp` | `/srv/omp/omp-config` |
| `HOST_UID` / `HOST_GID` | uid:gid, от которого контейнер пишет файлы (Linux; на macOS выравнивает virtiofs) | `1000` / `1000` |

## Диагностика

- `omp --version` внутри контейнера — бинарник жив, версия актуальна.
- `git config --system protocol.allow` → `never` — транспорт закрыт.
- `ls /workspace/repos` — монтирование репозиториев на месте.
- Файлы в примонтированном каталоге принадлежат root — на Linux-хосте `HOST_UID/HOST_GID` не заданы под владельца каталогов.
