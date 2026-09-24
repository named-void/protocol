CODEX_HOME ?= $(HOME)/.codex
CLAUDE_HOME ?= $(HOME)/.claude
KILO_CONFIG_HOME ?= $(HOME)/.config/kilo
CURSOR_HOME ?= $(HOME)/.cursor
OMP_HOME ?= $(HOME)/.omp
SKILLS_DIR := $(CURDIR)/skills
SKILL_LINKS := $(CURDIR)/scripts/manage-skill-links.sh
INSTRUCTION_LINK := $(CURDIR)/scripts/manage-instruction-link.sh
DOCTOR := $(CURDIR)/scripts/doctor.sh
SKILL_PAIRS := protocol $(SKILLS_DIR)/protocol \
	test-protocol $(SKILLS_DIR)/test-protocol \
	checker $(SKILLS_DIR)/checker \
	text-edit $(SKILLS_DIR)/text-edit \
	git-workflow $(SKILLS_DIR)/git-workflow \
	self-directed $(SKILLS_DIR)/self-directed \
	mreviewer $(SKILLS_DIR)/mreviewer \
	adapter-issues $(SKILLS_DIR)/adapter/issues \
	adapter-wiki $(SKILLS_DIR)/adapter/wiki \
	adapter-vcs $(SKILLS_DIR)/adapter/vcs
TEST_MODULES := $(patsubst tests/%.py,tests.%,$(wildcard tests/test_*.py))
TEST_JOBS ?= 6

.PHONY: help install uninstall doctor check test lint

.DEFAULT_GOAL := help

help: ## показать список целей
	@printf 'Доступные цели:\n'
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| sed -e 's/:.*## /\t/' \
		| sort \
		| awk -F'\t' '{ printf "  %-12s %s\n", $$1, $$2 }'

install: ## установить симлинки инструкций и навыков
	@"$(INSTRUCTION_LINK)" install "$(CODEX_HOME)/AGENTS.md" "$(CURDIR)/AGENTS.md"
	@"$(INSTRUCTION_LINK)" install "$(CLAUDE_HOME)/CLAUDE.md" "$(CURDIR)/AGENTS.md"
	@"$(INSTRUCTION_LINK)" install "$(KILO_CONFIG_HOME)/AGENTS.md" "$(CURDIR)/AGENTS.md"
	@"$(INSTRUCTION_LINK)" install "$(CURSOR_HOME)/AGENTS.md" "$(CURDIR)/AGENTS.md"
	@"$(INSTRUCTION_LINK)" install "$(OMP_HOME)/agent/AGENTS.md" "$(CURDIR)/AGENTS.md"
	@"$(SKILL_LINKS)" install "$(CODEX_HOME)/skills" $(SKILL_PAIRS)
	@"$(SKILL_LINKS)" install "$(CLAUDE_HOME)/skills" $(SKILL_PAIRS)
	@"$(SKILL_LINKS)" install "$(KILO_CONFIG_HOME)/skills" $(SKILL_PAIRS)
	@"$(SKILL_LINKS)" install "$(CURSOR_HOME)/skills" $(SKILL_PAIRS)
	@"$(SKILL_LINKS)" install "$(OMP_HOME)/agent/skills" $(SKILL_PAIRS)

uninstall: ## удалить симлинки инструкций и навыков
	@"$(INSTRUCTION_LINK)" uninstall "$(CODEX_HOME)/AGENTS.md" "$(CURDIR)/AGENTS.md"
	@"$(INSTRUCTION_LINK)" uninstall "$(CLAUDE_HOME)/CLAUDE.md" "$(CURDIR)/AGENTS.md"
	@"$(INSTRUCTION_LINK)" uninstall "$(KILO_CONFIG_HOME)/AGENTS.md" "$(CURDIR)/AGENTS.md"
	@"$(INSTRUCTION_LINK)" uninstall "$(CURSOR_HOME)/AGENTS.md" "$(CURDIR)/AGENTS.md"
	@"$(INSTRUCTION_LINK)" uninstall "$(OMP_HOME)/agent/AGENTS.md" "$(CURDIR)/AGENTS.md"
	@"$(SKILL_LINKS)" uninstall "$(CODEX_HOME)/skills" $(SKILL_PAIRS)
	@"$(SKILL_LINKS)" uninstall "$(CLAUDE_HOME)/skills" $(SKILL_PAIRS)
	@"$(SKILL_LINKS)" uninstall "$(KILO_CONFIG_HOME)/skills" $(SKILL_PAIRS)
	@"$(SKILL_LINKS)" uninstall "$(CURSOR_HOME)/skills" $(SKILL_PAIRS)
	@"$(SKILL_LINKS)" uninstall "$(OMP_HOME)/agent/skills" $(SKILL_PAIRS)

doctor: ## проверить станцию: версии, конфиг, MCP и симлинки
	@CODEX_HOME="$(CODEX_HOME)" CLAUDE_HOME="$(CLAUDE_HOME)" KILO_CONFIG_HOME="$(KILO_CONFIG_HOME)" \
	 CURSOR_HOME="$(CURSOR_HOME)" OMP_HOME="$(OMP_HOME)" \
	 bash "$(DOCTOR)" $(SKILL_PAIRS)

check: lint test ## прогнать проверки репозитория: lint и test

test: ## запустить unittest-модули параллельно (TEST_JOBS)
	@printf '%s\n' $(TEST_MODULES) | xargs -P $(TEST_JOBS) -I{} python3 -m unittest {}

lint: ## проверить shell (bash -n, shellcheck) и прогнать ruff
	@find "$(CURDIR)/scripts" "$(CURDIR)/skills" -name '*.sh' -exec bash -n {} +
	@if command -v shellcheck >/dev/null 2>&1; then \
		find "$(CURDIR)/scripts" "$(CURDIR)/skills" -name '*.sh' -exec shellcheck -S warning {} +; \
	else printf 'skip: shellcheck not installed\n'; fi
	@if command -v ruff >/dev/null 2>&1; then \
		ruff check "$(CURDIR)/lib" "$(CURDIR)/tests" "$(CURDIR)/skills"; \
	else printf 'skip: ruff not installed\n'; fi
