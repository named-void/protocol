#!/usr/bin/env python3
"""TOML configuration reader for protocol skills.

Configuration is the carrier map (override via ``AGENTS_CONFIG``); secrets live
in ``<data-root>/config.toml`` (override via ``AGENTS_SECRETS``). The source
checkout may live in ``<carrier>/src``; runtime remains outside Git in
``<carrier>/projects/<project>/.data`` for an identified project and
``<carrier>/projects/_common/.data`` outside one (override via
``AGENTS_DATA_DIR``).

CLI::

    python3 skills_config.py get <dotted.key> [--default X] [--json]
    python3 skills_config.py validate
    python3 skills_config.py export-env
    python3 skills_config.py project [--path <dir>]
    python3 skills_config.py projects-root
    python3 skills_config.py data-root
    python3 skills_config.py allocate-task-key <base> [--path <dir>]
    python3 skills_config.py roots [--project <name>]

``project`` prints the identified project and the channel that identified it.

Configuration is a single layer — the carrier map:
``<projects-root>/<name>/project.toml`` for an identified project and
``<projects-root>/_common/project.toml`` outside one.
It holds roots, adapter values and heartbeat thresholds; secrets (``[mcp.*]`` with ``url`` and ``http_headers``) live in the secrets file outside git and nowhere else.

``get`` prints the resolved value and exits 1 when the key is missing and
no default was supplied. ``validate`` type-checks the known keys of every
carrier map, requires the keys of ``REQUIRED_KEYS`` to be present, and prints
a ``fail:`` line for every mismatch or missing key. ``export-env``
prints shell ``export`` lines for the ``AGENTS_*`` variables the git-workflow
scripts read, so the orchestrator seeds them from config with one
``eval "$(... export-env)"`` instead of assembling each by hand — otherwise a
missing export silently falls back to the scripts' built-in defaults and
config diverges from behaviour.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import tomllib

COMMON_PROJECT = "_common"
# Ключ выделенной работы: ключ источника плюс индекс. Индекс продолжает
# максимальный уже видимый — в каталогах задач, ветках, worktree и истории.
DERIVED_INDEX_TEMPLATE = r"(?:^|[-/])%s-(\d+)$"


@functools.cache
def _git_main_repo() -> Path | None:
    # Корень ОСНОВНОГО репозитория правил. При запуске из git worktree
    # (.claude/worktrees/…) якорь по файлу указывает на worktree, а не на
    # основной checkout, и рантайм молча форкается в <worktree>/.data вместо
    # носителя. --git-common-dir из worktree даёт .git основного репозитория;
    # относительный путь трактуем от каталога скрипта. resolve() обязателен —
    # при вызове через симлинк скилл-инсталла якорь считается от реального
    # файла репозитория.
    script_dir = Path(__file__).resolve().parent
    try:
        out = subprocess.run(
            ["git", "-C", str(script_dir), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    if not out:
        return None
    common = Path(out)
    if not common.is_absolute():
        common = script_dir / common
    return common.resolve().parent


def _git_anchored_sibling(name: str) -> Path | None:
    repo = _git_main_repo()
    return None if repo is None else repo.parent / name


def rules_repo_root() -> Path:
    """Корень основного checkout репозитория правил — носитель его рантайма
    (orchestration/adr/README.md#runtime-store). Рантайм у репозитория один:
    worktree его не форкает."""
    anchored = _git_main_repo()
    if anchored is not None:
        return anchored
    return rules_tree_root()


def rules_tree_root() -> Path:
    """Корень рабочего дерева, из которого исполняется код protocol."""
    return Path(__file__).resolve().parents[1]


def runtime_carrier_root() -> Path:
    """Неверсионируемый носитель runtime.

    При разделённой установке Git-репозиторий правил находится в ``src/``, а
    runtime остаётся у его родителя. Старую плоскую установку поддерживаем,
    чтобы обновление можно было выполнить до физического переноса checkout.
    """
    source_root = rules_repo_root()
    return source_root.parent if source_root.name == "src" else source_root


def data_root() -> Path:
    """Корень рантайма периметра (orchestration/adr/README.md#runtime-store):
    ``<carrier>/projects/<проект>/.data`` для опознанного проекта,
    ``<carrier>/projects/_common/.data`` для работы вне проекта.
    ``$AGENTS_DATA_DIR`` сохраняет приоритет."""
    override = os.environ.get("AGENTS_DATA_DIR")
    if override:
        return Path(override)
    name, _ = identify_project()
    if name:
        return projects_root() / name / ".data"
    return projects_root() / COMMON_PROJECT / ".data"


def projects_root() -> Path:
    """Единый корень проектных карт, профилей и runtime вне ``src``."""
    override = os.environ.get("AGENTS_PROJECTS_DIR")
    if override:
        return Path(override)
    return runtime_carrier_root() / "projects"


def _git_lines(repository: Path, *arguments: str) -> list[str]:
    """Вывод git-команды построчно; недоступный репозиторий даёт пустой список."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return [line for line in completed.stdout.splitlines() if line]


def observed_task_keys(repository: Path) -> list[str]:
    """Всё, где уже мог остаться след ключа задачи: каталоги задач, ветки,
    worktree и заголовки коммитов. Индекс выделенной работы продолжает максимум
    именно по этому наблюдаемому состоянию, а не по отдельному счётчику."""
    observed: list[str] = []
    for tasks_dir in projects_root().glob("*/.data/tasks"):
        if tasks_dir.is_dir():
            observed.extend(entry.name for entry in tasks_dir.iterdir())
    observed.extend(
        _git_lines(repository, "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes")
    )
    observed.extend(
        line[len("worktree ") :]
        for line in _git_lines(repository, "worktree", "list", "--porcelain")
        if line.startswith("worktree ")
    )
    observed.extend(_git_lines(repository, "log", "--all", "--format=%s", "-n", "5000"))
    return observed


def allocate_task_key(base: str, path: str | os.PathLike[str] | None = None) -> str:
    """Выделить ключ `<base>-<N>` для работы, у которой своего ключа нет."""
    base = base.strip()
    if not base or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*", base):
        raise ConfigError(f"invalid task key base {base!r}")

    repository = Path(path).expanduser() if path else Path.cwd()
    pattern = re.compile(DERIVED_INDEX_TEMPLATE % re.escape(base))
    highest = 0
    for candidate in observed_task_keys(repository):
        for token in candidate.split():
            match = pattern.search(token)
            if match:
                highest = max(highest, int(match.group(1)))
    return f"{base}-{highest + 1}"


def secrets_path() -> Path:
    """Файл секретов периметра — `[mcp.*]` с `url` и `http_headers`
    (orchestration/adr/README.md#mcp-credentials). Лежит в рантайме носителя,
    вне git; `$AGENTS_SECRETS` перекрывает путь."""
    override = os.environ.get("AGENTS_SECRETS")
    if override:
        return Path(override)
    return data_root() / "config.toml"


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


class ConfigError(RuntimeError):
    """Конфигурация невалидна: работа по ней не начинается."""


def project_names() -> list[str]:
    root = projects_root()
    if not root.is_dir():
        return []
    return sorted(entry.name for entry in root.iterdir() if (entry / "project.toml").is_file())


def project_config_path(name: str) -> Path:
    return projects_root() / name / "project.toml"


def carrier_config_path(name: str | None) -> Path:
    """Карта носителя (orchestration/adr/README.md#carrier-config): `project.toml`
    в git проекта, а для работы вне опознанного проекта — карта `_common`.
    `$AGENTS_CONFIG` перекрывает резолв целиком — карта и есть конфигурация
    периметра."""
    override = os.environ.get("AGENTS_CONFIG")
    if override:
        return Path(override)
    return project_config_path(name or COMMON_PROJECT)


def load_carrier_map(path: Path) -> dict[str, Any]:
    """Карта носителя целиком. Секреты в ней запрещены: право объявить `url` и
    `http_headers` превратило бы карту в канал случайной утечки personal-token;
    их место в файле секретов `.data/config.toml`."""
    config = _load(path)
    if "mcp" in config:
        raise ConfigError(
            f"{path}: [mcp.*] holds secrets and belongs to {secrets_path()},"
            " not to a versioned carrier map"
        )
    return config


def load_carrier_config(name: str | None) -> dict[str, Any]:
    return load_carrier_map(carrier_config_path(name))


def _roots_of(config: dict[str, Any], name: str) -> list[Path]:
    declared = config.get("project", {}).get("roots", [])
    if isinstance(declared, str) or not isinstance(declared, list):
        raise ConfigError(f"{project_config_path(name)}: project.roots must be a list of paths")
    base = project_config_path(name).parent
    roots = []
    for root in declared:
        path = Path(str(root)).expanduser()
        roots.append(path if path.is_absolute() else (base / path).resolve())
    return roots


def project_roots(name: str) -> list[Path]:
    """Корни проекта в файловой системе — канал его опознания и целевой
    каталог для clone отсутствующего репозитория (SB-96). Относительный путь
    разрешается от каталога `project.toml`."""
    return _roots_of(load_carrier_config(name), name)


def resolve_vcs_project(host: str, repository_path: str) -> dict[str, Any]:
    """Resolve a VCS repository to one project carrier and local checkout."""
    normalized_host = host.strip().lower()
    normalized_path = repository_path.strip().strip("/").removesuffix(".git")
    matches: list[tuple[int, str, str, dict[str, Any]]] = []
    for name in project_names():
        path = project_config_path(name)
        try:
            carrier = load_carrier_map(path)
        except Exception as exc:  # noqa: BLE001 - broken neighbor is diagnosed separately
            print(f"warn: project '{name}': {exc}", file=sys.stderr)
            continue
        hosts = carrier.get("vcs_host", {}).get("hosts", [])
        mapping = carrier.get("dispatch", {}).get("key_prefix_map", {})
        if (
            isinstance(hosts, str)
            or not isinstance(hosts, list)
            or normalized_host not in {str(item).lower() for item in hosts}
            or not isinstance(mapping, dict)
        ):
            continue
        for namespace in {str(item).strip().strip("/") for item in mapping.values()}:
            if normalized_path == namespace or normalized_path.startswith(namespace + "/"):
                matches.append((len(namespace), name, namespace, carrier))
    if not matches:
        raise ConfigError(f"no project mapping for {normalized_host}/{normalized_path}")
    longest = max(item[0] for item in matches)
    winners = [item for item in matches if item[0] == longest]
    if len(winners) != 1:
        names = ", ".join(sorted({item[1] for item in winners}))
        raise ConfigError(
            f"ambiguous project mapping for {normalized_host}/{normalized_path}: {names}"
        )
    _, name, namespace, carrier = winners[0]
    roots = _roots_of(carrier, name)
    if not roots:
        raise ConfigError(f"{project_config_path(name)}: project.roots is empty")
    relative = normalized_path.removeprefix(namespace).lstrip("/")
    local_path = roots[0] / relative if relative else roots[0]
    return {
        "name": name,
        "namespace": namespace,
        "local_path": local_path.resolve(),
    }


def _project_of_path(path: Path, *, reject_ambiguity: bool = False) -> str | None:
    # Самый глубокий корень выигрывает: вложенные проекты разрешаются в пользу
    # ближайшего вверх, как и требует опознание по рабочему каталогу.
    target = path.expanduser().resolve()
    best: tuple[int, str] | None = None
    tied_with_best: str | None = None
    for name in project_names():
        # Опознание читает карты всех проектов и потому смотрит только на
        # `project.roots`: нечитаемая карта соседа роняла бы любой вызов
        # конфига где угодно; пропускаем её, но не молча — `validate` и
        # `make doctor` печатают fail.
        try:
            roots = _roots_of(_load(project_config_path(name)), name)
        except Exception as exc:  # noqa: BLE001 - битая карта соседа не наша беда
            print(f"warn: project '{name}': {exc}", file=sys.stderr)
            continue
        for root in roots:
            resolved = root.resolve()
            if target != resolved and resolved not in target.parents:
                continue
            depth = len(resolved.parts)
            if best is None or depth > best[0]:
                best = (depth, name)
                tied_with_best = None
            elif reject_ambiguity and depth == best[0] and name != best[1]:
                tied_with_best = name
    if reject_ambiguity and best is not None and tied_with_best is not None:
        raise ConfigError(
            f"ambiguous project roots for path {target}: {best[1]}, {tied_with_best}"
        )
    return best[1] if best else None


def project_from_path(path: str | os.PathLike[str]) -> str | None:
    """Identify a project only from an explicit path, ignoring runtime overrides."""
    return _project_of_path(Path(str(path)), reject_ambiguity=True)


def _project_of_key(key: str) -> str | None:
    """Проект по префиксу ключа задачи: маршрут начинается с ключа, когда
    рабочего каталога ещё нет (confidence gate adapter-vcs выбирает, куда
    клонировать). Читается тот же `dispatch.key_prefix_map` карты."""
    prefix = key.split("-", 1)[0].strip().upper()
    if not prefix:
        return None
    matches: list[str] = []
    for name in project_names():
        try:
            mapping = _load(project_config_path(name)).get("dispatch", {}).get("key_prefix_map", {})
        except Exception as exc:  # noqa: BLE001 - битая карта соседа не наша беда
            print(f"warn: project '{name}': {exc}", file=sys.stderr)
            continue
        if isinstance(mapping, dict) and any(str(k).upper() == prefix for k in mapping):
            matches.append(name)
    if len(matches) > 1:
        raise ConfigError(
            f"ambiguous project mapping for issue prefix {prefix}: {', '.join(matches)}"
        )
    return matches[0] if matches else None


def identify_project(
    path: str | os.PathLike[str] | None = None, key: str | None = None
) -> tuple[str | None, str]:
    """Проект и канал опознания (orchestration/adr/README.md#project-layer):
    явный override (`$AGENTS_PROJECT`) → целевой путь работы (аргумент или
    `$AGENTS_PROJECT_PATH`) → префикс ключа задачи → ближайший вверх корень от
    рабочего каталога → «вне проекта». Работа над самим оркестратором штатно
    попадает в последнюю ветвь: проектная специфика ей не нужна."""
    override = os.environ.get("AGENTS_PROJECT")
    if override:
        if override not in project_names():
            raise ConfigError(
                f"AGENTS_PROJECT={override!r}: no project card at {project_config_path(override)}"
            )
        return override, "override"
    target = path if path is not None else os.environ.get("AGENTS_PROJECT_PATH")
    if target:
        name = _project_of_path(Path(str(target)))
        if name:
            return name, "target-path"
    if key:
        name = _project_of_key(key)
        if name:
            return name, "issue-key"
    name = _project_of_path(Path.cwd())
    if name:
        return name, "cwd"
    return None, "outside"


def load_secrets() -> dict[str, Any]:
    """Секции `[mcp.<сервер>]` файла секретов носителя. Всё прочее в нём —
    невыполненная миграция станционного слоя, а не конфигурация: значения
    оттуда не читаются, `validate` называет их fail. Скаляр под корнем `mcp`
    сервером не является и тоже не читается."""
    servers = _load(secrets_path()).get("mcp", {})
    if not isinstance(servers, dict):
        return {}
    return {name: server for name, server in servers.items() if isinstance(server, dict)}


def load_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Конфигурация периметра одним слоем
    (orchestration/adr/README.md#carrier-config): карта носителя плюс его
    секреты. Слоя над периметром нет — отсутствующий ключ берёт дефолт у своего
    потребителя."""
    name, _ = identify_project(path)
    config = load_carrier_config(name)
    secrets = load_secrets()
    if secrets:
        config = {**config, "mcp": secrets}
    return config


def _get(config: dict[str, Any], dotted: str) -> Any:
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(dotted)
        node = node[part]
    return node


def get_section(dotted: str) -> dict[str, Any]:
    """Секция-таблица конфига по dotted-ключу — для потребителей, которым нужна
    структура, а не скаляр (`get_value`). KeyError, если ключа нет или он не
    таблица: вызывающий сам решает, это блокер или необязательная настройка."""
    value = _get(load_config(), dotted)
    if not isinstance(value, dict):
        raise KeyError(f"key '{dotted}' is not a table")
    return value


def get_value(dotted: str, default: str | None = None) -> str:
    config = load_config()
    try:
        value = _get(config, dotted)
    except KeyError:
        if default is not None:
            return default
        raise
    if isinstance(value, (dict, list)):
        raise TypeError(f"key '{dotted}' is structured, not a scalar")
    return str(value)


def adapter_is_off(product: str) -> bool:
    """Выключен ли адаптер класса при таком значении `<class>.product`.

    Значение читают клиенты адаптеров и гейт `scripts/check-mcp.py`: разная
    нормализация в них означала бы, что один конфиг выключает адаптер в одном
    месте и оставляет сетевой вызов в другом (SB-210).
    """
    return product.strip().lower() in ("", "none")


EXPECTED_TYPES: dict[str, list[str]] = {
    "scalar": [
        "issue_tracker.product",
        "issue_tracker.tool_prefix",
        "issue_tracker.my_issues_query",
        "issue_tracker.key_pattern",
        "issue_tracker.default_fields",
        "issue_tracker.fields.sprint",
        # Имена заголовков прямого REST-чтения remote links задачи: у них есть
        # рабочие дефолты, поэтому обязательными не являются — типизируются,
        # чтобы неверное значение падало на гейте, а не на вызове.
        "issue_tracker.rest.url_header",
        "issue_tracker.rest.token_header",
        "vcs_host.product",
        "docs_wiki.product",
        "docs_wiki.tool_prefix",
        "docs_wiki.spaces",
        # Имена заголовков прямого REST-чтения статуса резолюции комментариев:
        # дефолты рабочие, поэтому ключи не обязательны.
        "docs_wiki.rest.url_header",
        "docs_wiki.rest.token_header",
        "vcs.commit_title_format",
        # Базовый интервал heartbeat сессии; остальные пороги строятся его
        # коэффициентами.
        "thresholds.heartbeat_seconds",
        # Коэффициент тишины до ping.
        "thresholds.heartbeat_ping_multiplier",
        # Множитель heartbeat для порога stale/died.
        "thresholds.heartbeat_dead_multiplier",
    ],
    "list": [
        "vcs.branch_types",
        # Корни, исключённые из spec-discovery целиком (черновики аналитиков):
        # URL или page id, сравнение идёт по id.
        "docs_wiki.spec_exclude",
        "vcs_host.hosts",
        "mreviewer.rules",
    ],
    "table": [
        "dispatch.key_prefix_map",
        "mreviewer",
        # Доступ к MCP-серверам (orchestration/adr/README.md#mcp-credentials):
        # секция на сервер, имя — продукт адаптера, поэтому типизируется
        # корень, а не отдельные секции.
        "mcp",
    ],
    "tables": [
        "profile_rules",
        "docs_wiki.spec_roots",
    ],
}


def _is_type(value: Any, type_name: str) -> bool:
    if type_name == "scalar":
        return not isinstance(value, (dict, list))
    if type_name == "list":
        return isinstance(value, list) and not any(isinstance(e, dict) for e in value)
    if type_name == "table":
        return isinstance(value, dict)
    if type_name == "tables":
        return isinstance(value, list) and all(isinstance(e, dict) for e in value)
    return False


# Ключи без работоспособного фолбэка: без них маршрут встаёт перед commit.
REQUIRED_KEYS: tuple[str, ...] = (
    "vcs.commit_title_format",
)


def cmd_validate(_args: list[str]) -> int:
    checked = 0
    failures: list[str] = []
    secrets_file_path = secrets_path()
    try:
        secrets_file = _load(secrets_file_path)
    except Exception as exc:  # noqa: BLE001 - surface any parse failure
        print(f"fail: {secrets_file_path}: {exc}")
        return 1
    # Файл секретов несёт только `[mcp.*]`: прочие секции в нём — след
    # упразднённого станционного слоя, чьи значения рантайм уже не читает
    # (orchestration/adr/README.md#carrier-config).
    stale = sorted(key for key in secrets_file if key != "mcp")
    servers = secrets_file.get("mcp", {})
    if isinstance(servers, dict):
        # Скаляр под корнем `mcp` сервером не является: без этой проверки
        # посторонний ключ проходил гейт и читался как значение конфигурации.
        stale.extend(
            f"mcp.{name}" for name, server in sorted(servers.items()) if not isinstance(server, dict)
        )
    elif "mcp" in secrets_file:
        stale.append("mcp")
    if stale:
        failures.append(
            f"fail: {secrets_file_path}: only [mcp.<server>] sections belong here;"
            f" move to the carrier map: {', '.join(stale)}"
        )

    # Карты валидируются все, а не только карта носителя этой работы: битая
    # карта соседа ломает работу в его каталоге, и узнать об этом на гейте
    # дешевле, чем посреди маршрута.
    try:
        identified = identify_project()[0]
    except Exception as exc:  # noqa: BLE001 - surface parse and schema failures
        failures.append(f"fail: {exc}")
        identified = None
    own_path = carrier_config_path(identified)
    config: dict[str, Any] = {}
    seen: set[Path] = set()
    for label, path in [
        *((f"project '{name}'", project_config_path(name)) for name in project_names()),
        ("carrier", own_path),
    ]:
        if path in seen:
            continue
        seen.add(path)
        try:
            carrier = load_carrier_map(path)
        except Exception as exc:  # noqa: BLE001 - surface any parse failure
            failures.append(f"fail: {label}: {exc}")
            continue
        if path == own_path:
            config = carrier
    if secrets_file.get("mcp"):
        config = {**config, "mcp": secrets_file["mcp"]}
    for key in REQUIRED_KEYS:
        try:
            _get(config, key)
        except KeyError:
            failures.append(f"fail: {key}: required key is missing")
    for type_name, keys in EXPECTED_TYPES.items():
        for key in keys:
            try:
                value = _get(config, key)
            except KeyError:
                continue
            checked += 1
            if not _is_type(value, type_name):
                failures.append(f"fail: {key}: expected {type_name}")

    for line in failures:
        print(line)
    if failures:
        return 1
    print(f"ok: {checked} keys checked")
    return 0


def cmd_get(args: list[str]) -> int:
    usage = "usage: skills_config.py get <dotted.key> [--default X] [--json]"
    # Флаги позиционно-независимы: ключ — первый неопциональный токен,
    # --default/--json в любом порядке до или после него (SB-27).
    key: str | None = None
    default: str | None = None
    as_json = False
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--json":
            as_json = True
        elif token == "--default":
            if i + 1 >= len(args):
                print(usage, file=sys.stderr)
                return 2
            default = args[i + 1]
            i += 1
        elif token.startswith("--"):
            print(f"error: unknown option '{token}'\n{usage}", file=sys.stderr)
            return 2
        elif key is None:
            key = token
        else:
            print(f"error: unexpected argument '{token}'\n{usage}", file=sys.stderr)
            return 2
        i += 1
    if key is None:
        print(usage, file=sys.stderr)
        return 2

    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001 - surface any parse failure
        print(f"error: cannot load config: {exc}", file=sys.stderr)
        return 1
    try:
        value = _get(config, key)
    except KeyError:
        if default is not None:
            print(default)
            return 0
        # Назови проверенный путь: «ключ не найден, потому что конфиг не там»
        # не должно быть молчаливым.
        carrier_p = carrier_config_path(identify_project()[0])
        secrets_p = secrets_path()
        print(
            f"error: key '{key}' not found and no default supplied; checked "
            f"{carrier_p} ({'ok' if carrier_p.is_file() else 'missing'}) and "
            f"{secrets_p} ({'ok' if secrets_p.is_file() else 'missing'})",
            file=sys.stderr,
        )
        return 1

    if as_json:
        print(json.dumps(value, ensure_ascii=False))
        return 0
    if isinstance(value, (dict, list)):
        print(f"error: key '{key}' is structured, not a scalar", file=sys.stderr)
        return 1
    print(str(value))
    return 0


# AGENTS_* переменные, которые git-workflow читает поверх встроенных дефолтов. `is_list` склеивает элементы списка через пробел; `AGENTS_BASE_BRANCH` задаёт вызывающий.
EXPORT_ENV: list[tuple[str, str, bool]] = [
    ("AGENTS_KEY_PATTERN", "issue_tracker.key_pattern", False),
    ("AGENTS_BRANCH_TYPES", "vcs.branch_types", True),
]


def cmd_export_env(_args: list[str]) -> int:
    config = load_config()
    for env_name, dotted, is_list in EXPORT_ENV:
        try:
            value = _get(config, dotted)
        except KeyError:
            # Ключа нет — не печатаем строку: скрипт применит свой дефолт, а
            # молчаливая пустая переменная перекрыла бы его пустым значением.
            continue
        if is_list:
            if not isinstance(value, list):
                print(f"error: key '{dotted}' expected list", file=sys.stderr)
                return 1
            rendered = " ".join(str(element) for element in value)
        else:
            if isinstance(value, (dict, list)):
                print(f"error: key '{dotted}' is structured, not a scalar", file=sys.stderr)
                return 1
            rendered = str(value)
        print(f"export {env_name}={shlex.quote(rendered)}")
    return 0


def _dotted_leaves(node: dict[str, Any], prefix: str = "") -> list[str]:
    keys: list[str] = []
    for key, value in node.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            keys.extend(_dotted_leaves(value, f"{dotted}."))
        else:
            keys.append(dotted)
    return keys


def cmd_project(args: list[str]) -> int:
    """Опознанный проект и канал опознания одной строкой: `<имя> <канал>`
    либо `- outside`. С `--keys` — ещё и ключи карты носителя, по строке на
    ключ. Потребитель — оркестратор и `make doctor`: работа вне проекта обязана
    быть видимой, а не тихой."""
    usage = "usage: skills_config.py project [--path <dir>] [--key <ISSUE-KEY>] [--keys]"
    path: str | None = None
    key: str | None = None
    keys = False
    i = 0
    while i < len(args):
        if args[i] in ("--path", "--key"):
            if i + 1 >= len(args):
                print(usage, file=sys.stderr)
                return 2
            if args[i] == "--path":
                path = args[i + 1]
            else:
                key = args[i + 1]
            i += 1
        elif args[i] == "--keys":
            keys = True
        else:
            print(f"error: unexpected argument '{args[i]}'\n{usage}", file=sys.stderr)
            return 2
        i += 1
    try:
        name, channel = identify_project(path, key)
        carrier = load_carrier_config(name)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{name or '-'} {channel}")
    if keys:
        for dotted in sorted(_dotted_leaves(carrier)):
            print(dotted)
    return 0


def cmd_roots(args: list[str]) -> int:
    """Корни проекта в файловой системе, по строке на корень. Первый корень —
    целевой каталог clone и рабочий каталог периметра, поэтому его читают и
    маршрут adapter-vcs, и запуск контейнера; собирать его разбором карты каждому
    потребителю значило бы держать вторую копию правила."""
    usage = "usage: skills_config.py roots [--project <name>]"
    name: str | None = None
    i = 0
    while i < len(args):
        if args[i] == "--project":
            if i + 1 >= len(args):
                print(usage, file=sys.stderr)
                return 2
            name = args[i + 1]
            i += 1
        else:
            print(f"error: unexpected argument '{args[i]}'\n{usage}", file=sys.stderr)
            return 2
        i += 1
    try:
        if name is None:
            name = identify_project()[0]
        if name is None:
            print("error: no project identified and --project not given", file=sys.stderr)
            return 1
        if name not in project_names():
            raise ConfigError(f"no project card at {project_config_path(name)}")
        for root in project_roots(name):
            print(root)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_data_root(_args: list[str]) -> int:
    """Корень рантайма носителя одной строкой — резолв для shell-потребителей
    (`scripts/doctor.sh`): второй копии правила `.data` в скриптах быть не
    должно."""
    print(data_root())
    return 0


def cmd_projects_root(_args: list[str]) -> int:
    """Единый корень карт, профилей и runtime проектных периметров."""
    print(projects_root())
    return 0


def cmd_allocate_task_key(args: list[str]) -> int:
    """Выделить ключ выделенной работы и вывести его одной строкой."""
    base = ""
    path: str | None = None
    rest = list(args)
    while rest:
        item = rest.pop(0)
        if item == "--path":
            if not rest:
                print("error: --path requires a value", file=sys.stderr)
                return 2
            path = rest.pop(0)
        elif not base:
            base = item
        else:
            print(f"error: unexpected argument {item!r}", file=sys.stderr)
            return 2
    if not base:
        print("error: task key base is required", file=sys.stderr)
        return 2
    try:
        print(allocate_task_key(base, path))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def profiles_for_path(path: str | os.PathLike[str] | None = None) -> list[Path]:
    """Return applicable project and technology profiles for a target path."""
    raw_target = Path(path).expanduser() if path else Path.cwd()
    targets = {str(raw_target), str(raw_target.resolve())}
    name, _ = identify_project(path, None)
    if not name:
        return []
    layer = load_carrier_config(name)
    rules = layer.get("profile_rules") or []
    if not isinstance(rules, list):
        raise ConfigError(f"{project_config_path(name)}: profile_rules must be a list")
    project_dir = projects_root() / name
    repo_root = Path(__file__).resolve().parents[1]
    seen: set[str] = set()
    result: list[Path] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        match = str(rule.get("match", "")).strip()
        if match and not match.startswith("path:"):
            print(
                f"warn: {project_config_path(name)}: profile_rules match "
                f"{match!r} is not 'path:<fragment>', rule skipped",
                file=sys.stderr,
            )
            continue
        fragment = match[len("path:"):]
        if fragment and not any(fragment in candidate for candidate in targets):
            continue
        entries = rule.get("profiles") or []
        if isinstance(entries, str) or not isinstance(entries, list):
            raise ConfigError(f"{project_config_path(name)}: profiles must be a list of paths")
        for entry in entries:
            raw = str(entry).strip()
            if not raw:
                continue
            base = project_dir if raw.startswith("./") else repo_root
            resolved = str((base / raw).resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            result.append(Path(resolved))
    return result


def mreviewer_rules(project_name: str) -> list[Path]:
    """Return review-only rules configured for one project carrier."""
    layer = load_carrier_config(project_name)
    section = layer.get("mreviewer") or {}
    if not isinstance(section, dict):
        raise ConfigError(f"{project_config_path(project_name)}: mreviewer must be a table")
    entries = section.get("rules", [])
    if isinstance(entries, str) or not isinstance(entries, list):
        raise ConfigError(
            f"{project_config_path(project_name)}: mreviewer.rules must be a list"
        )
    project_dir = projects_root() / project_name
    repo_root = Path(__file__).resolve().parents[1]
    seen: set[str] = set()
    result: list[Path] = []
    for entry in entries:
        raw = str(entry).strip()
        if not raw:
            continue
        base = project_dir if raw.startswith("./") else repo_root
        resolved = str((base / raw).resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(Path(resolved))
    return result


def cmd_profiles(args: list[str]) -> int:
    """Применимые документы профилей для целевого пути — по строке на путь,
    в порядке их объявления в карте проекта.

    Резолв `profile_rules` (orchestration/adr/README.md#project-layer) один для
    всех потребителей: оркестратор читает профили сам, манифест участника
    конвейера получает те же пути строками (SB-179). Правило применяется, когда
    фрагмент его `match = "path:<фрагмент>"` входит в целевой путь — как
    переданный, так и разрешённый по симлинкам (на macOS `/var` — симлинк на
    `/private/var`, и карта проекта записана по одному из двух). Пустой
    `match` применяется всегда, `match` неизвестного вида — не применяется, но
    называется в stderr. `./<имя>` резолвится от каталога карты проекта,
    `profiles/<имя>` — от корня репозитория навыков. Вне проекта вывод пуст.
    """
    usage = "usage: skills_config.py profiles [--path <dir>]"
    path: str | None = None
    i = 0
    while i < len(args):
        if args[i] == "--path":
            if i + 1 >= len(args):
                print(usage, file=sys.stderr)
                return 2
            path = args[i + 1]
            i += 1
        else:
            print(f"error: unexpected argument '{args[i]}'\n{usage}", file=sys.stderr)
            return 2
        i += 1
    try:
        for profile in profiles_for_path(path):
            print(profile)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    args = argv[1:]
    if args and args[0] == "get":
        return cmd_get(args[1:])
    if args and args[0] == "validate":
        return cmd_validate(args[1:])
    if args and args[0] == "export-env":
        return cmd_export_env(args[1:])
    if args and args[0] == "project":
        return cmd_project(args[1:])
    if args and args[0] == "profiles":
        return cmd_profiles(args[1:])
    if args and args[0] == "data-root":
        return cmd_data_root(args[1:])
    if args and args[0] == "projects-root":
        return cmd_projects_root(args[1:])
    if args and args[0] == "allocate-task-key":
        return cmd_allocate_task_key(args[1:])
    if args and args[0] == "roots":
        return cmd_roots(args[1:])
    print(
        "usage: skills_config.py get <dotted.key> [--default X] [--json]"
        " | validate | export-env"
        " | project [--path <dir>] | profiles [--path <dir>]"
        " | projects-root | data-root | allocate-task-key <base> [--path <dir>]"
        " | roots [--project <name>]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
