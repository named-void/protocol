# Особенности kilo

- Kilo 7.4.20 поддерживает native-сабагентов: `kilo agent create --mode
  subagent`, встроенные `explore` и `general` имеют режим `subagent`, а primary-
  агент вызывает их инструментом `task`. Модель собственного сабагента задаётся
  в его профиле через `kilo agent create -m …`.
- Канала hooks у CLI нет (`kilo --help`): подмешать что-либо в контекст сессии
  нечем, весь durable-контекст приходит сводом правил и навыками.
- Каталог конфигурации выбирается `XDG_CONFIG_HOME`: под подменённым значением
  `kilo debug config` печатает другой резолвнутый конфиг — без секции
  `provider`, то есть без ключей (проверено на подмене и на контроле). Изоляция
  user-слоя строится оверлеем: каталог симлинков на всё содержимое штатного
  `~/.config/kilo`, кроме `AGENTS.md` и `skills`.
- `permission` в `~/.config/kilo/kilo.jsonc` выбирается `findLast`: побеждает
  последнее совпавшее правило, дефолт без совпадений — `ask`. Катч-олл `"*"` —
  первым ключом, специфичные `allow` — после.
- `edit` матчится по относительному пути от cwd, абсолютные glob не совпадают;
  запись вне cwd гейтится `external_directory`, где паттерны абсолютные.
  Сужение записи вешать на `external_directory`, `edit` держать `allow`.
- `kilo run --auto` — auto-approve всего, конфиг не действует;
  `--dangerously-skip-permissions` уважает явные `deny`.
- Проверка: `kilo run -m openrouter/deepseek/deepseek-v4-flash "создай файл X"` —
  headless `ask` печатает `permission requested: …; auto-rejecting`;
  `kilo debug config` показывает резолвнутый конфиг.
- `bash: allow` пишет мимо edit-правил: настоящая граница — только
  OS-песочница.
- Идентификатор модели состоит из трёх сегментов:
  `-m openrouter/deepseek/deepseek-v4-flash`. Двухсегментная форма падает
  `ProviderModelNotFoundError` со списком suggestions.
- Все четыре корня состояния берутся из переменных XDG (`kilo debug paths`):
  `data`, `config`, `cache`, `state`. Ими же CLI уводится с состояния станции на
  своё.
- Без `rg` в `PATH` CLI качает свою копию в `$XDG_CACHE_HOME/kilo/bin` при первом
  запуске: без сети файловые инструменты отказывают `Unexpected error`, с
  системным `rg` работают.
