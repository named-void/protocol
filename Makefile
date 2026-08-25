# Прокси к основному Makefile в src/: цели выполняются там же, где лежат скрипты.
MAKEFLAGS += --no-print-directory

.DEFAULT_GOAL := help

.PHONY: help
help:
	@$(MAKE) -C src help

# Любая другая цель делегируется в src/Makefile без дублирования списка.
%:
	@$(MAKE) -C src $@
