# Окружение запуска задач omp (protocol, PRT-docker-1).
# Минимальный официальный образ; omp — релизный бинарник GitHub releases
# (can1357/oh-my-pi), sha256 из SHA256SUMS.txt релиза; brew не используется.
# Репозитории и конфигурация провайдеров в образ не входят — приезжают
# bind-mount'ом (compose.yaml). Git remote-транспорт запрещён системным
# gitconfig: pull/push делает только хост.
FROM alpine:3.22

# git — рабочий инструмент задач (ветки, worktree, коммиты), python3 —
# сценарии окружения (tomllib требует 3.11+), ripgrep/jq/curl/less — файловые
# инструменты CLI и адаптеры.
RUN apk add --no-cache \
        bash \
        ca-certificates \
        curl \
        git \
        jq \
        less \
        python3 \
        ripgrep

ARG OMP_VERSION=18.1.14
ARG OMP_SHA256_ARM64=b5401be8afee11bd706b452660844101985f89774ecb2aeee03d588d60b942aa
ARG OMP_SHA256_AMD64=617d67627154f2e1a07d5bae36e190a47240a55379a3308cd61e7f62b4ce2e82
ARG TARGETARCH
# Релизный бинарник по платформе сборки; несоответствие чексуммы или сбой
# запуска ломает сборку, а не кладёт в образ неисполнимый файл.
RUN set -eu; \
    case "${TARGETARCH}" in \
        arm64) name="omp-linux-musl-arm64"; sha256="${OMP_SHA256_ARM64}" ;; \
        amd64) name="omp-linux-musl-x64";  sha256="${OMP_SHA256_AMD64}" ;; \
        *) printf 'omp: сборки под платформу %s нет\n' "${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/omp \
        "https://github.com/can1357/oh-my-pi/releases/download/v${OMP_VERSION}/${name}"; \
    printf '%s  /tmp/omp\n' "${sha256}" | sha256sum -c -; \
    install -m 0755 /tmp/omp /usr/local/bin/omp; \
    rm -f /tmp/omp; \
    omp --version

# pull/push/fetch из контейнера запрещены на уровне транспорта; локальные
# операции и worktree работают. Синхронизацию с remote выполняет хост.
RUN git config --system protocol.allow never

# Байткод python в примонтированные деревья не пишем.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/agent

# Непривилегированный пользователь по умолчанию; compose задаёт user: хоста.
RUN adduser -D -h /home/agent agent

WORKDIR /workspace
CMD ["sleep", "infinity"]
