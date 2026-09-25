---
name: audio-generation
description: 'Триггеры: точное имя audio-generation либо явная команда настроить, проверить
  или перенести генерацию речи (TTS: локальный Silero v4 и/или OpenRouter tts-cloud)
  по этому навыку. Документирует постоянный стек генерации: CLI tts (офлайн) и tts-cloud
  (стриминг по предложениям), утверждённые модели и голоса. Проверено на omp 18.2.11,
  macOS Apple Silicon (M4), Homebrew 7.'
---

# Audio generation (генерация речи: Silero v4 + OpenRouter)

Архитектура: два CLI в `~/.local/bin` (симлинки в `~/.local/silero/bin/`):

- `tts` - локальный синтез Silero v4 (русский, офлайн, голос kseniya по умолчанию,
  48 кГц, RTF 0.01-0.05 на M4). Модель `v4_ru.pt` (38 МБ) грузится через
  `torch.package.PackageImporter` - **не через torch.hub**: репозиторий
  `snakers4/silero-tts` не существует (404), hub ломается на GitHub-валидации.
- `tts-cloud` - облачный синтез через OpenRouter `POST /api/v1/audio/speech`
  (ключ `~/.config/openrouter/key`, права 600). Стриминг по предложениям:
  кусок 1 играет сразу, остальные синтезируются в фоне (prefetch); ретраи ×3
  с бэкоффом; `TTFS_DEBUG=1` - тайминги в stderr. gemini/qwen отдают raw PCM
  24 кГц - автоматически оборачивается в WAV.

## Финальный список моделей и голосов (утверждён пользователем)

| `-m` | Модель | Голос | Формат |
|---|---|---|---|
| `grok` (дефолт) | x-ai/grok-voice-tts-1.0 | eve | mp3 |
| `gemini` | google/gemini-3.8-flash-tts | Zephyr | pcm→wav |
| `qwenplus` | qwen/qwen-audio-3.0-tts-plus | longanlingxin | pcm→wav |

Локально: `tts` - Silero v4, голос kseniya (дефолт). Прочие Silero-голоса
(aidar, baya, eugene, xenia, random) доступны, не исключались.

**Исключено пользователем (не возвращать без явной команды):** fish-audio/s2.1-pro;
qwen-flash (loongjohn, longanhuan_v3.6 - мужские); grok ara/rex/sal/leo; qwen
longanlufeng. У gemini мужские тембры (Puck, Charon, Fenrir) не тестировались
на слух - при чистке ориентироваться на пользователя.

Валидные голоса провайдера смотреть в живом каталоге, не угадывать:
`GET https://openrouter.ai/api/v1/models?output_modalities=speech` → поле
`supported_voices`. Известные ограничения: minimax - только `English_*`-пресеты,
mai-voice-2 - локали en/es-MX/fr-FR/de-DE (русского нет), StreamElements/Polly
требует ключ (маршрут мёртв). Безключевая альтернатива вне omp - edge-tts
(Azure Neural, ru-RU-SvetlanaNeural/DmitryNeural).

## Установка (один блок, идемпотентный)

```bash
set -euo pipefail
brew list python@3.13 >/dev/null 2>&1 || brew install python@3.13
mkdir -p ~/.local/silero/bin ~/.local/silero/samples ~/.local/bin
[ -d ~/.local/silero/venv ] || /opt/homebrew/bin/python3.13 -m venv ~/.local/silero/venv
~/.local/silero/venv/bin/pip install -q numpy torch
[ -s ~/.local/silero/v4_ru.pt ] || \
  curl -fL https://models.silero.ai/models/tts/ru/v4_ru.pt -o ~/.local/silero/v4_ru.pt
ln -sf ~/.local/silero/bin/tts ~/.local/bin/tts
ln -sf ~/.local/silero/bin/tts-cloud ~/.local/bin/tts-cloud
# исходники bin/tts и bin/tts-cloud — в секции «Инструменты» ниже,
# развернуть их в ~/.local/silero/bin/ и chmod +x
command -v tts-cloud >/dev/null || echo 'PATH должен содержать ~/.local/bin'
```

Требования: Homebrew, ключ OpenRouter в `~/.config/openrouter/key` (600),
`~/.local/bin` в PATH. ffmpeg НЕ нужен (воспроизведение - afplay).

## Инструменты

### ~/.local/silero/bin/tts

```python
#!/Users/i/.local/silero/venv/bin/python
"""tts — локальный синтез речи Silero v4 (офлайн, по умолчанию голос kseniya).

Примеры:
  tts "текст"                # синтез и воспроизведение
  tts -v baya "текст"        # другой голос
  tts -o out.wav "текст"     # записать wav вместо воспроизведения
  echo "текст" | tts         # текст из stdin
"""
import argparse
import os
import subprocess
import sys
import tempfile
import wave

import torch

MODEL = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "v4_ru.pt")
SR = 48000

def main() -> None:
    ap = argparse.ArgumentParser(description="Локальный TTS Silero v4 (русский).")
    ap.add_argument("-v", "--voice", default="kseniya",
                    choices=["aidar", "baya", "eugene", "kseniya", "xenia", "random"])
    ap.add_argument("-o", "--out", help="записать wav в файл вместо воспроизведения")
    ap.add_argument("text", nargs="*", help="текст; иначе читается stdin")
    a = ap.parse_args()
    text = " ".join(a.text).strip() or sys.stdin.read().strip()
    if not text:
        sys.exit("tts: пустой текст")

    model = torch.package.PackageImporter(MODEL).load_pickle("tts_models", "model")
    audio = model.apply_tts(text=text, speaker=a.voice, sample_rate=SR, put_accent=True, put_yo=True)
    pcm = (audio * 32767.0).clamp(-32768, 32767).to(torch.int16).cpu().numpy().tobytes()

    if a.out:
        path = a.out
    else:
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)

    if a.out:
        print(path)
    else:
        subprocess.run(["afplay", path], check=True)
        os.unlink(path)

if __name__ == "__main__":
    main()
```

### ~/.local/silero/bin/tts-cloud

```python
#!/Users/i/.local/silero/venv/bin/python
"""tts-cloud — облачный синтез через OpenRouter со стримингом по предложениям.

Модели (-m), проверенные на этой машине (мужские голоса исключены пользователем):
  grok     x-ai/grok-voice-tts-1.0        голос: eve (единственный)
  gemini   google/gemini-3.8-flash-tts    голос: Zephyr (единственный)
  qwenplus qwen/qwen-audio-3.0-tts-plus   голос: longanlingxin (единственный)

Примеры:
  tts-cloud "текст"                     # grok, голос eve, играть
  tts-cloud -m gemini "текст"           # переключиться на gemini/Zephyr
  tts-cloud -m qwenplus "текст"         # qwenplus/longanlingxin
  tts-cloud -m gemini -o out.wav "текст"  # весь текст одним файлом
  TTFS_DEBUG=1 tts-cloud "..."          # тайминги в stderr

gemini и qwen отдают raw PCM 24 кГц — автоматически оборачивается в WAV.
"""
import argparse
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import wave

MODELS = {
    "grok":   {"id": "x-ai/grok-voice-tts-1.0",      "voice": "eve",       "fmt": "mp3"},
    # голоса grok ara/rex/sal/leo исключены пользователем; осталась только eve.
    # fish (fish-audio/s2.1-pro) исключён решением пользователя; не добавлять без явной команды.
    "gemini": {"id": "google/gemini-3.8-flash-tts",  "voice": "Zephyr",    "fmt": "pcm"},
    "qwenplus": {"id": "qwen/qwen-audio-3.0-tts-plus", "voice": "longanlingxin", "fmt": "pcm"},
    # qwen-flash (loongjohn, longanhuan_v3.6) исключена: мужские голоса, решено пользователем.
}
KEY_PATH = os.path.expanduser("~/.config/openrouter/key")
DEBUG = os.environ.get("TTFS_DEBUG")
T0 = time.perf_counter()

def log(msg: str) -> None:
    if DEBUG:
        print(f"[{time.perf_counter() - T0:5.2f}s] {msg}", file=sys.stderr)

def split_sentences(text: str, min_len: int = 60) -> list[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", text) if p.strip()]
    chunks: list[str] = []
    for p in parts:  # короткие предложения склеиваем для естественной интонации
        if chunks and len(chunks[-1]) + len(p) + 1 < min_len:
            chunks[-1] += " " + p
        else:
            chunks.append(p)
    return chunks

def synth(model_id: str, voice: str | None, text: str, fmt: str, attempts: int = 3) -> bytes:
    key = open(KEY_PATH).read().strip()
    payload: dict = {"model": model_id, "input": text, "response_format": fmt}
    if voice:
        payload["voice"] = voice
    last: Exception = RuntimeError("нет попыток")
    for n in range(attempts):
        try:
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/audio/speech",
                data=json.dumps(payload).encode(),
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                         "User-Agent": "tts-cloud/1.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                audio = r.read()
            if len(audio) < 1000:
                raise RuntimeError(f"короткий ответ ({len(audio)} b): {audio[:200]!r}")
            return audio
        except Exception as e:
            last = e
            log(f"попытка {n+1} не удалась: {e}")
            time.sleep(1.5 * (n + 1))
    raise last

def write_audio(data: bytes, base: str, fmt: str) -> str:
    ext = ".wav" if fmt == "pcm" else ".mp3"
    path = base if base.endswith(ext) else base + ext
    if fmt == "pcm":  # gemini/qwen: raw PCM 24 кГц 16-bit mono -> wav
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(data)
    else:
        open(path, "wb").write(data)
    return path

def main() -> None:
    ap = argparse.ArgumentParser(description="Облачный TTS со стримингом (OpenRouter).",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="Модели: " + ", ".join(MODELS))
    ap.add_argument("-m", "--model", default="grok", choices=list(MODELS))
    ap.add_argument("-v", "--voice", default=None,
                    help="голос модели; по умолчанию — дефолт выбранной модели")
    ap.add_argument("-o", "--out", help="записать весь текст одним файлом вместо стриминга")
    ap.add_argument("text", nargs="*", help="текст; иначе читается stdin")
    a = ap.parse_args()
    spec = MODELS[a.model]
    voice = a.voice if a.voice is not None else spec["voice"]
    text = " ".join(a.text).strip() or sys.stdin.read().strip()
    if not text:
        sys.exit("tts-cloud: пустой текст")

    if a.out:  # файл целиком — один запрос, без стриминга
        print(write_audio(synth(spec["id"], voice, text, spec["fmt"]), a.out, spec["fmt"]))
        return

    chunks = split_sentences(text)
    if not chunks:
        sys.exit("tts-cloud: пустой текст")
    log(f"модель {a.model}, кусков: {len(chunks)}")

    results: queue.Queue = queue.Queue()

    def worker() -> None:
        for i, ch in enumerate(chunks):
            try:
                t = time.perf_counter()
                audio = synth(spec["id"], voice, ch, spec["fmt"])
                log(f"куск {i+1}: {len(audio)} b за {time.perf_counter() - t:.2f}s")
                results.put((i, audio))
            except Exception as e:
                results.put((i, e))
        results.put(None)  # sentinel: воркер закончил

    threading.Thread(target=worker, daemon=True).start()

    tmpdir = os.environ.get("TMPDIR", "/tmp")
    expected = 0
    failures = 0
    try:
        while expected < len(chunks):
            item = results.get()
            if item is None:
                break
            i, audio = item
            if isinstance(audio, Exception):
                print(f"tts-cloud: кусок {i+1}: {audio}", file=sys.stderr)
                failures += 1
                expected += 1
                continue
            fd, base = tempfile.mkstemp(prefix=f"or_{a.model}_", dir=tmpdir)
            os.close(fd)
            path = write_audio(audio, base, spec["fmt"])
            log(f"куск {i+1}: play")
            subprocess.run(["afplay", path], check=False)
            os.unlink(path)
            expected += 1
    except KeyboardInterrupt:
        pass
    sys.exit(1 if failures else 0)

if __name__ == "__main__":
    main()
```

## Приёмка на целевой машине

1. `tts --help` - без ошибок; `tts-cloud --help` - строка "Модели: grok, gemini, qwenplus".
2. `tts -v kseniya "проверка"` - локальное воспроизведение.
3. `TTFS_DEBUG=1 tts-cloud "два предложения. Второе длиннее."` - первый кусок
   ≤3 с (здоровый канал; при деградации сети провайдер отвечает 40+ с - это
   не баг стриминга).
4. `~/.config/openrouter/key` существует, права 600.

## Интеграция с omp

- Встроенный инструмент `tts` omp (`speechgen.enabled: true`) маршрутизируется
  `providers.tts`: `local` (Kokoro-82M, голос `tts.localVoice` - русского нет),
  `xai` (Grok Voice eve - тот же голос, нужен xAI-кред), `deepinfra` (нужен
  DEEPINFRA_API_KEY). Голоса OpenRouter (Zephyr, longanlingxin) штатно недостижимы.
- Голоса из списка выше агент вызывает через bash (`tts` / `tts-cloud`);
  инструкция закреплена в AGENTS.md, раздел "Озвучка".
- Настройки omp (`speech.*`, `stt.*`, `live.*`) этим навыком не изменяются.

## Откат

1. `rm ~/.local/bin/tts ~/.local/bin/tts-cloud && rm -rf ~/.local/silero`.
2. Ключ `~/.config/openrouter/key` удалить или оставить (не связан со стеком).
3. Конфиг omp не изменялся; навык нигде не регистрируется.
