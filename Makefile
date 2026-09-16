PY ?= $(HOME)/python/global/bin/python
BRANCH ?= dev

.PHONY: check rebuild test project

check:        ## promotion gate: chain, replay, projections, evidence, cheap anchors
	$(PY) engine.py check

rebuild:      ## rebuild a branch from canonical events (never main): make rebuild BRANCH=dev
	$(PY) engine.py rebuild --branch $(BRANCH)

project:
	$(PY) engine.py project

test:
	$(PY) -m pytest tests -q
