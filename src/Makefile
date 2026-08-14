CODEX_HOME ?= $(HOME)/.codex
CLAUDE_HOME ?= $(HOME)/.claude
KILO_CONFIG_HOME ?= $(HOME)/.config/kilo
CURSOR_HOME ?= $(HOME)/.cursor
SKILLS_DIR := $(CURDIR)/skills
SKILL_LINKS := $(CURDIR)/scripts/manage-skill-links.sh
INSTRUCTION_LINK := $(CURDIR)/scripts/manage-instruction-link.sh
SKILL_PAIRS := protocol $(SKILLS_DIR)/protocol \
	checker $(SKILLS_DIR)/checker \
	text-edit $(SKILLS_DIR)/text-edit \
	git-workflow $(SKILLS_DIR)/git-workflow \
	self-directed $(SKILLS_DIR)/self-directed \
	mreviewer $(SKILLS_DIR)/mreviewer \
	adapter-issues $(SKILLS_DIR)/adapter/issues \
	adapter-wiki $(SKILLS_DIR)/adapter/wiki \
	adapter-vcs $(SKILLS_DIR)/adapter/vcs
TEST_MODULES := $(filter-out tests.test_vcs_scripts,$(patsubst tests/%.py,tests.%,$(wildcard tests/test_*.py)))
TEST_JOBS ?= 6

.PHONY: install uninstall test test-vcs lint

install:
	@"$(INSTRUCTION_LINK)" install "$(CODEX_HOME)/AGENTS.md" "$(CURDIR)/AGENTS.md"
	@"$(INSTRUCTION_LINK)" install "$(CLAUDE_HOME)/CLAUDE.md" "$(CURDIR)/AGENTS.md"
	@"$(INSTRUCTION_LINK)" install "$(KILO_CONFIG_HOME)/AGENTS.md" "$(CURDIR)/AGENTS.md"
	@"$(INSTRUCTION_LINK)" install "$(CURSOR_HOME)/AGENTS.md" "$(CURDIR)/AGENTS.md"
	@"$(SKILL_LINKS)" install "$(CODEX_HOME)/skills" $(SKILL_PAIRS)
	@"$(SKILL_LINKS)" install "$(CLAUDE_HOME)/skills" $(SKILL_PAIRS)
	@"$(SKILL_LINKS)" install "$(KILO_CONFIG_HOME)/skills" $(SKILL_PAIRS)
	@"$(SKILL_LINKS)" install "$(CURSOR_HOME)/skills" $(SKILL_PAIRS)

uninstall:
	@"$(INSTRUCTION_LINK)" uninstall "$(CODEX_HOME)/AGENTS.md" "$(CURDIR)/AGENTS.md"
	@"$(INSTRUCTION_LINK)" uninstall "$(CLAUDE_HOME)/CLAUDE.md" "$(CURDIR)/AGENTS.md"
	@"$(INSTRUCTION_LINK)" uninstall "$(KILO_CONFIG_HOME)/AGENTS.md" "$(CURDIR)/AGENTS.md"
	@"$(INSTRUCTION_LINK)" uninstall "$(CURSOR_HOME)/AGENTS.md" "$(CURDIR)/AGENTS.md"
	@"$(SKILL_LINKS)" uninstall "$(CODEX_HOME)/skills" $(SKILL_PAIRS)
	@"$(SKILL_LINKS)" uninstall "$(CLAUDE_HOME)/skills" $(SKILL_PAIRS)
	@"$(SKILL_LINKS)" uninstall "$(KILO_CONFIG_HOME)/skills" $(SKILL_PAIRS)
	@"$(SKILL_LINKS)" uninstall "$(CURSOR_HOME)/skills" $(SKILL_PAIRS)

test:
	@printf '%s\n' $(TEST_MODULES) | xargs -P $(TEST_JOBS) -I{} python3 -m unittest {}

test-vcs:
	python3 -m unittest tests.test_vcs_scripts

lint:
	@find "$(CURDIR)/scripts" "$(CURDIR)/skills" -name '*.sh' -exec bash -n {} +
	@if command -v ruff >/dev/null 2>&1; then \
		ruff check "$(CURDIR)/lib" "$(CURDIR)/tests" "$(CURDIR)/skills"; \
	else printf 'skip: ruff not installed\n'; fi
