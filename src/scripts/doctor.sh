#!/usr/bin/env bash
# Диагностика станции: версии инструментов, конфигурация носителя, доступность
# MCP-серверов и симлинки инструкций и навыков в каталогах CLI. Read-only:
# ничего не чинит, печатает по строке на проверку и возвращает 1, если есть
# хотя бы один `fail:`.
set -uo pipefail

repo_root=$(cd "$(dirname "$0")/.." && pwd -P)

CODEX_HOME=${CODEX_HOME:-$HOME/.codex}
CLAUDE_HOME=${CLAUDE_HOME:-$HOME/.claude}
KILO_CONFIG_HOME=${KILO_CONFIG_HOME:-$HOME/.config/kilo}
CURSOR_HOME=${CURSOR_HOME:-$HOME/.cursor}
OMP_HOME=${OMP_HOME:-$HOME/.omp}

# Реестр точек входа приходит из Makefile теми же парами `<имя> <путь>`, что
# получает manage-skill-links.sh: второй копии списка навыков здесь быть не
# должно, иначе doctor проверяет не то, что ставит install.
[[ $(( $# % 2 )) -eq 0 ]] || {
  printf 'usage: %s [<link-name> <source-path> ...]\n' "$0" >&2
  exit 2
}
skill_names=()
skill_sources=()
while [[ $# -gt 0 ]]; do
  skill_names+=("$1")
  skill_sources+=("$2")
  shift 2
done

fail=0

if python3 -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)"; then
  echo "ok: python3 >= 3.11"
else
  echo "fail: python3 < 3.11"; fail=1
fi

gitver=$(git --version | awk '{print $3}')
if python3 -c "import sys; v='$gitver'.split('.'); g=(int(v[0]), int(v[1]) if len(v) > 1 else 0); sys.exit(0 if g >= (2, 31) else 1)"; then
  echo "ok: git >= 2.31 ($gitver)"
else
  echo "fail: git < 2.31 ($gitver)"; fail=1
fi

if command -v shellcheck >/dev/null 2>&1; then
  echo "ok: shellcheck installed"
else
  echo "warn: shellcheck not installed"
fi

# Файл секретов: дефолт — <data-root>/config.toml, корень носителя резолвит сам
# конфиг-модуль (`skills_config.py data-root`), второй копии правила `.data`
# здесь нет.
config=${AGENTS_SECRETS:-$(python3 "$repo_root/lib/skills_config.py" data-root)/config.toml}

# Карты носителей проверяются всегда: они версионируются и живут в дереве, а
# отсутствие файла секретов — состояние новой машины, а не повод пропустить
# проверку конфигурации. Строки validate печатаются как есть: он называет
# отсутствующий ключ, несовпадение типа или сломанную карту.
if validate_out=$(AGENTS_SECRETS="$config" python3 "$repo_root/lib/skills_config.py" validate 2>&1); then
  echo "ok: config validates"
else
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    case "$line" in
      fail:*|warn:*) echo "$line" ;;
      *) echo "fail: config: $line" ;;
    esac
  done <<< "$validate_out"
  fail=1
fi

if [[ -f "$config" ]]; then
  echo "ok: secrets file exists ($config)"
  # Креды MCP живут в секциях [mcp.*] файла секретов: без этой проверки первая
  # ошибка авторизации всплывает только в работе адаптера. Печатаются имя
  # сервера и исход — URL, заголовки и токены не выводятся никогда.
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    case "$line" in
      ok:*|skip:*|warn:*) echo "$line" ;;
      *) echo "$line"; fail=1 ;;
    esac
  done < <(AGENTS_SECRETS="$config" python3 "$repo_root/scripts/check-mcp.py" 2>/dev/null)
else
  # `.data` носителя вне git, поэтому на новой машине файла секретов нет:
  # называем путь, а не только диагноз.
  echo "warn: secrets file not found ($config)"
  echo "run: создай его вручную — секции [mcp.<продукт>] с url и заголовками"
fi

# Проектный слой не переносится с клоном исходников: на новой станции проекты
# клонируются отдельно. Работа вне проекта штатна, но должна быть видимой.
projects_root=$(AGENTS_SECRETS="$config" python3 "$repo_root/lib/skills_config.py" projects-root)
if [[ -d "$projects_root" ]]; then
  found=0
  for card in "$projects_root"/*/project.toml; do
    [[ -f "$card" ]] && found=$((found + 1))
  done
  echo "ok: projects root ($projects_root): $found project card(s)"
else
  echo "warn: no projects root ($projects_root); project specifics are not loaded"
fi

project_err=$(mktemp)
if project_line=$(AGENTS_SECRETS="$config" python3 "$repo_root/lib/skills_config.py" project --keys 2>"$project_err"); then
  project_name=$(printf '%s\n' "$project_line" | head -1 | awk '{print $1}')
  project_channel=$(printf '%s\n' "$project_line" | head -1 | awk '{print $2}')
  project_keys=$(printf '%s\n' "$project_line" | tail -n +2 | tr '\n' ' ')
  if [[ "$project_name" == "-" ]]; then
    echo "ok: no project identified for $PWD (outside; rules repository carrier)"
  else
    echo "ok: project '$project_name' identified by $project_channel"
    echo "ok: keys of the carrier map: ${project_keys:-none}"
  fi
else
  echo "fail: carrier config: $project_line $(tr '\n' ' ' < "$project_err")"; fail=1
fi
rm -f "$project_err"

check_cli() {
  local cli=$1 skill_dir=$2 instr_link=$3
  if [[ ! -d "$skill_dir" ]]; then
    echo "warn: $cli not installed"
    return
  fi

  local tgt
  if [[ -L "$instr_link" ]]; then
    tgt=$(readlink "$instr_link")
    if [[ "$tgt" == "$repo_root/AGENTS.md" ]]; then
      echo "ok: $cli instruction link"
    else
      echo "warn: $cli instruction link is a foreign symlink -> $tgt"
    fi
  elif [[ -f "$instr_link" ]]; then
    echo "warn: $cli instruction link is a regular file"
  else
    echo "warn: $cli instruction link missing"
  fi

  local i name link source
  for i in "${!skill_names[@]}"; do
    name=${skill_names[$i]}
    source=${skill_sources[$i]}
    link="$skill_dir/$name"
    if [[ -L "$link" ]]; then
      tgt=$(readlink "$link")
      if [[ "$tgt" != "$source" ]]; then
        echo "fail: $cli: foreign skill link $link -> $tgt"; fail=1
      elif [[ ! -e "$link" ]]; then
        echo "fail: $cli: dangling skill link $link -> $tgt"; fail=1
      fi
      continue
    fi
    if [[ ! -e "$link" ]]; then
      echo "fail: $cli: missing skill link $link"; fail=1
    fi
  done

  # Обратный обход: переименование каталога навыка оставляет плоский симлинк со
  # старым именем, а обход по реестру видит только объявленные имена, и мусор
  # остаётся в каталоге CLI молча. Проверяются только ссылки внутрь repo_root —
  # каталог навыков CLI общий, чужие ссылки не наши.
  local known=" ${skill_names[*]:-} "
  for link in "$skill_dir"/*; do
    [[ -L "$link" ]] || continue
    tgt=$(readlink "$link")
    case "$tgt" in
      "$repo_root"|"$repo_root"/*) ;;
      *) continue ;;
    esac
    name=$(basename "$link")
    case "$known" in
      *" $name "*) continue ;;
    esac
    if [[ -e "$link" ]]; then
      echo "fail: $cli: unregistered skill link $link -> $tgt"; fail=1
    else
      echo "fail: $cli: dangling skill link $link -> $tgt"; fail=1
    fi
  done
}

check_cli codex "$CODEX_HOME/skills" "$CODEX_HOME/AGENTS.md"
check_cli claude "$CLAUDE_HOME/skills" "$CLAUDE_HOME/CLAUDE.md"
check_cli kilo "$KILO_CONFIG_HOME/skills" "$KILO_CONFIG_HOME/AGENTS.md"
check_cli cursor "$CURSOR_HOME/skills" "$CURSOR_HOME/AGENTS.md"

# omp потребляет собственный нативный слой (свод и навыки в OMP_HOME/agent);
# кодекс-совместимые провайдеры правил на станции выключены.
check_cli omp "$OMP_HOME/agent/skills" "$OMP_HOME/agent/AGENTS.md"

if command -v omp >/dev/null 2>&1; then
  echo "ok: omp installed ($(omp --version 2>/dev/null | awk -F/ '{print $2}'))"
else
  echo "warn: omp not installed"
fi
if [[ -f "$OMP_HOME/agent/config.yml" ]]; then
  echo "ok: omp station config ($OMP_HOME/agent/config.yml)"
else
  echo "warn: omp station config missing ($OMP_HOME/agent/config.yml)"
fi

exit $fail
