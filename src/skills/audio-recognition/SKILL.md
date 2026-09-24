---
name: audio-recognition
description: 'Триггеры: точное имя audio-recognition либо явная команда установить или перенести
  локальную диктовку omp через whisper.cpp на macOS по этому навыку. Устанавливает
  whisper.cpp-сервер как LaunchAgent и подключает его к роли dictation omp через
  расширение-провайдер. Проверено на omp 18.2.11, whisper.cpp 1.9.4, macOS / Apple Silicon.'
---

# Audio recognition (локальная диктовка omp через whisper.cpp)

Архитектура: удержание `Space` в TUI omp → запись → POST multipart на
`http://127.0.0.1:9176/v1/audio/transcriptions` (whisper.cpp server, OpenAI-совместимый
маршрут) → текст в редактор. Встроенные `local/whisper-*` omp не используются: они
исполняются через transformers.js ONNX q8 принудительно на CPU (omp сворачивает GPU на
Darwin из-за крэша onnxruntime в Bun), качество и скорость ниже whisper.cpp ggml на Metal.

## 1. Установка (один блок, идемпотентный)

Целевая платформа: macOS на Apple Silicon (проверено на M4, Metal). Нужны Homebrew и
omp; `command -v` резолвит путь whisper-server, ручных замен путей нет.

```bash
set -euo pipefail

# preflight
command -v brew >/dev/null || { echo "ERROR: нет Homebrew"; exit 1; }
command -v omp   >/dev/null || { echo "ERROR: нет omp - подключать не к чему"; exit 1; }
echo "omp: $(omp --version 2>/dev/null || echo '?') | arch: $(uname -m) | macOS: $(sw_vers -productVersion)"

brew install whisper.cpp
WHISPER_SERVER=$(command -v whisper-server)

# модели: атомарно (временный файл + mv) и с проверкой минимального размера -
# обрезанный при сбое .bin иначе остался бы навсегда и валил LaunchAgent циклом
mkdir -p "$HOME/.whisper-models"
fetch_model() {
  local url=$1 dst=$2 min=$3
  if [[ -s $dst ]] && (( $(stat -f%z "$dst") >= min )); then
    echo "skip: $dst"
    return 0
  fi
  rm -f "$dst"
  curl -fL "$url" -o "$dst.tmp" && test -s "$dst.tmp" \
    && (( $(stat -f%z "$dst.tmp") >= min )) && mv "$dst.tmp" "$dst" \
    || { rm -f "$dst.tmp"; echo "ERROR: download failed or truncated: $url"; return 1; }
}
M=https://huggingface.co/ggerganov/whisper.cpp/resolve/main
# точная модель (548 МБ); полегче - ggml-small.bin (466 МБ, минимум 400000000)
fetch_model "$M/ggml-large-v3-turbo-q5_0.bin" \
  "$HOME/.whisper-models/ggml-large-v3-turbo-q5_0.bin" 500000000
# silero VAD (865 КБ); репозиторий ggml-org/whisper-vad - в ggerganov/whisper.cpp его НЕТ
fetch_model https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin \
  "$HOME/.whisper-models/ggml-silero-vad.bin" 800000

# plist через heredoc: пути подставляются, не редактируются руками
mkdir -p "$HOME/Library/LaunchAgents"
PLIST="$HOME/Library/LaunchAgents/local.whisper-server.plist"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>local.whisper-server</string>
    <key>ProgramArguments</key>
    <array>
        <string>$WHISPER_SERVER</string>
        <string>-m</string><string>$HOME/.whisper-models/ggml-large-v3-turbo-q5_0.bin</string>
        <string>--host</string><string>127.0.0.1</string>
        <string>--port</string><string>9176</string>
        <string>-l</string><string>ru</string>
        <string>--vad</string>
        <string>-vm</string><string>$HOME/.whisper-models/ggml-silero-vad.bin</string>
        <string>--inference-path</string><string>/v1/audio/transcriptions</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ProcessType</key><string>Background</string>
    <key>StandardOutPath</key><string>/tmp/whisper-server.log</string>
    <key>StandardErrorPath</key><string>/tmp/whisper-server.log</string>
</dict>
</plist>
EOF

launchctl bootout gui/$(id -u)/local.whisper-server 2>/dev/null || true
sleep 1  # без паузы bootstrap после bootout падает с "5: Input/output error"
launchctl bootstrap gui/$(id -u) "$PLIST" \
  || { sleep 2; launchctl bootstrap gui/$(id -u) "$PLIST"; }

# readiness: холодный старт = загрузка модели + первая компиляция Metal-шейдеров (~20 с)
for _ in $(seq 1 40); do
  curl -s -o /dev/null --max-time 1 http://127.0.0.1:9176/ && break
  sleep 1
done
curl -s -o /dev/null --max-time 2 http://127.0.0.1:9176/ \
  || { echo "ERROR: сервер не поднялся, см. /tmp/whisper-server.log"; exit 1; }
echo "OK: whisper-server готов на 127.0.0.1:9176"
```

## 2. Приёмка сервера

```bash
say -o /tmp/t.aiff "Speech recognition check one two three." \
  && afconvert -f WAVE -d LEI16@16000 -c 1 /tmp/t.aiff /tmp/t.wav \
  && curl -s -X POST http://127.0.0.1:9176/v1/audio/transcriptions \
       -F file=@/tmp/t.wav -F model=x
# → {"text":" Speech Recognition Check 123\n"} (числа могут выходить цифрами - это -l ru)
```

Фраза на английском намеренно: дефолтный голос `say` на любой локали её озвучит;
русская фраза без `-v <русский голос>` превращается в неразборчивый звук, и whisper
тогда галлюцинирует посторонний текст. Первый запуск компилирует Metal-шейдеры ~20 с
(одноразово, общий кэш для всех бинарников whisper.cpp), далее меньше секунды на фразу.
Формат аудио omp не важен: сервер декодирует через miniaudio.

Настройки качества (правка plist → `launchctl kickstart -k gui/$(id -u)/local.whisper-server`):
модель `-m` (small быстрее, large-v3-turbo-q5_0 точнее); `-l ru` фиксирует язык
(английскую речь транскрибирует корректно, проверено; `auto` нужен при диктовке на
нескольких языках, но на фразах короче 3 с возможны промахи детекта); `--vad`/`-vm`
срезают тишину (начало фразы не обрезают, галлюцинаций `[музыка]` нет - проверено).

## 3. Подключение к omp

`~/.omp/agent/extensions/whisper-local-provider.ts`:

```ts
// Registers the local whisper.cpp server (OpenAI-compatible /v1/audio/transcriptions)
// under the built-in "local" provider: its catalog rule maps model ids matching
// whisper-* to catalog kind stt, which the dictation role requires.
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  pi.registerProvider("local", {
    baseUrl: "http://127.0.0.1:9176/v1",
    api: "openai-completions",
    apiKey: "no-key-needed",
    models: [
      {
        id: "whisper-cpp-metal",
        name: "whisper.cpp (Metal)",
        api: "openai-transcriptions",
      },
    ],
  });
}
```

`~/.omp/agent/config.yml` (сливать в существующие секции; дубликат корневого ключа
`retry:` недопустим):

```yaml
modelRoles:
  dictation: local/whisper-cpp-metal
retry:
  fallbackChains:
    dictation: []   # без тихого отката на встроенный parakeet
```

Механика (важно для отладки; проверено на omp 18.2.11 - при мажорном обновлении omp
перепроверить пункты 1 и 2):

1. Роль `dictation` принимает только модели с catalog kind `stt`; селектор с kind `chat`
   отбрасывается МОЛЧА - в логах ничего нет, omp берёт встроенный fallback
   `parakeet-tdt-0.6b-v3`. Кастомный провайдер kind `stt` получить нельзя: поле `kind`
   игнорируется и в `models.yml`, и в `registerProvider`. Обход - провайдер `local`:
   его каталогное KDL-правило ставит kind `stt` всем id по глобу `whisper-*`.
2. Диспетчеризация - по `api: openai-transcriptions` (HTTP) или `api: local-inference`
   (встроенный ONNX-воркер). `models.yml` такой api отвергает схемой (enum только
   чат-транспортов), путь расширения enum не валидирует - потому расширение, не yaml.
3. `--inference-path` обязателен: omp шлёт POST на `{baseUrl}/audio/transcriptions`,
   baseUrl с `/v1` в конце.
4. Расширение и роли читаются ТОЛЬКО при старте omp - после установки нужен рестарт
   сессии, действующая сессия продолжает старый движок.

## 4. Приёмка на целевой машине

1. `omp models --kind stt` - модель `whisper-cpp-metal` в группе `local`, конфиг без
   warnings (строки "Warning: models.yml validation failed" быть не должно).
2. Перезапустить omp, удержать `Space`, сказать фразу, отпустить.
3. Подтверждение маршрута: в `~/.omp/logs/<свежий>.log` - `STT live recording started`
   c `modelKey` НЕ parakeet/whisper-small; в `/tmp/whisper-server.log` -
   `operator(): processing ...`.

## 5. Откат

1. `launchctl bootout gui/$(id -u)/local.whisper-server` и удалить plist.
2. Удалить `~/.omp/agent/extensions/whisper-local-provider.ts`.
3. В config.yml: `dictation: local/whisper-small` (omp скачает ONNX-модель при первом
   использовании), `fallbackChains.dictation` убрать.
4. `brew uninstall whisper.cpp`, `rm -rf ~/.whisper-models`.
