"""Единый label участника/сессии из executor-spec (#participant-label).

Одна реализация правила формирования label — её зовут и оркестратор, и
скрипты (`run-executor.sh` fallback), чтобы артефакты исполнителей и имена
веток/воркдеревьев не расходились
(правило и обоснование: orchestration/adr/README.md#participant-label).

Правило label(spec), spec = AGENT[:MODEL[:EFFORT]]:
  1. AGENT, MODEL (опц.), EFFORT (только явный сегмент low|medium|high|xhigh|max,
     распознаётся так же, как в launch-session.sh — только в форме
     AGENT:MODEL:EFFORT);
  2. SHORT_MODEL_NAME: пусто при отсутствии MODEL; иначе часть MODEL после
     последнего '/', канонизированная под [a-z0-9-] (lowercase, ран вне
     [a-z0-9] -> '-', срез '-' по краям), со снятым ведущим '<agent>-';
  3. label = AGENT[-SHORT_MODEL_NAME][-EFFORT].

CLI:
  participant_label.py format <spec>       -> label одной строкой
"""

from __future__ import annotations

import re
import sys

# Шкала дублирована в launch-session.sh (case по последнему сегменту): launcher
# обязан разбирать spec на одном bash, без зависимости от интерпретатора.
# Расхождение ловит tests/test_participant_label.py — нераспознанный уровень
# молча уезжает в имя модели, а флаг CLI теряется.
EFFORT_SCALE = ("low", "medium", "high", "xhigh", "max")


def validate_spec(spec: str) -> None:
    if (
        not spec
        or any(char.isspace() for char in spec)
        or "::" in spec
        or spec.startswith(":")
        or spec.endswith(":")
    ):
        raise ValueError(
            f"invalid executor spec '{spec}' (expected AGENT[:MODEL[:EFFORT]], "
            "no spaces or empty segments)"
        )


def parse_spec(spec: str) -> tuple[str, str, str]:
    """AGENT, MODEL, EFFORT из spec — по той же логике, что launch-session.sh.

    EFFORT извлекается только из формы AGENT:MODEL:EFFORT (сегмент после
    первого ':' сам содержит ':'); в AGENT:EFFORT последний сегмент остаётся
    моделью — так же, как в launcher.
    """
    agent = spec.split(":", 1)[0]
    model = ""
    effort = ""
    if ":" in spec:
        model = spec.split(":", 1)[1]
        if ":" in model:
            last = model.rsplit(":", 1)[1]
            if last in EFFORT_SCALE:
                effort = last
                model = model.rsplit(":", 1)[0]
    return agent, model, effort


def canon(value: str) -> str:
    """lowercase; каждый ран символов вне [a-z0-9] -> один '-'; срез краёв."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def short_model_name(agent: str, model: str) -> str:
    if not model:
        return ""
    tail = canon(model.rsplit("/", 1)[-1])
    prefix = canon(agent) + "-"
    if tail.startswith(prefix):
        tail = tail[len(prefix):]
    return tail


def label(spec: str) -> str:
    validate_spec(spec)
    agent, model, effort = parse_spec(spec)
    parts = [canon(agent)]
    short = short_model_name(agent, model)
    if short:
        parts.append(short)
    if effort:
        parts.append(effort)
    result = "-".join(p for p in parts if p)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", result):
        raise ValueError(f"spec '{spec}' yields invalid label '{result}'")
    return result


def _main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: participant_label.py format <spec>", file=sys.stderr)
        return 64
    command, arg = argv
    if command == "format":
        try:
            print(label(arg))
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 65
        return 0
    print(f"error: unknown command '{command}'", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
