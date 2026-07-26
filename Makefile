PY   ?= python3
VENV := .venv
VPY  := $(VENV)/bin/python

# Strip any inherited virtualenv vars. `make install` is commonly run from
# inside another activated venv (edpapp/.venv), and a leaked VIRTUAL_ENV or
# PYTHONPATH sends pip's installs somewhere other than the venv we just made --
# the install reports success and the first import fails.
PIP := env -u VIRTUAL_ENV -u PYTHONHOME -u PYTHONPATH $(VPY) -m pip

.PHONY: install test eval eval-api probe fair-housing report doctor pii clean

install:
	$(PY) -m venv --clear $(VENV)
	$(PIP) install --upgrade pip setuptools wheel
	$(PIP) install -r requirements.txt
	$(PIP) install -e . --no-deps
	@$(VPY) -c "import yaml, pydantic, rich, anthropic; print('deps ok')"
	@echo "installed → $(VPY)"

# Free, deterministic, no credentials. This is what CI runs.
test: pii
	$(VPY) -m pytest tests/ -q
	$(VPY) -m edp_evals.runner --adapter stub

# Datasets are grown from real inbound mail. This is the control that keeps a
# careless copy-paste from publishing a real prospect's name.
pii:
	$(VPY) scripts/check_no_pii.py

# Full suite against the real deployed SKILL.md prompt, billed to your Claude
# Pro/Max subscription via the `claude` CLI. No API key needed.
eval:
	$(VPY) -m edp_evals.runner --adapter claude-code

# Safety gates only -- fast pre-deploy check after any prompt edit.
fair-housing:
	$(VPY) -m edp_evals.runner --adapter claude-code --tags fair_housing

# One case, with the raw CLI output shown. Start here to confirm the plumbing.
probe:
	$(VPY) -m edp_evals.runner --adapter claude-code --case fh_esa_on_no_pet_unit --verbose

# Same suite via the Anthropic API instead. Needs ANTHROPIC_API_KEY.
eval-api:
	$(VPY) -m edp_evals.runner --adapter claude --model claude-sonnet-5

report:
	@cat reports/latest.md

# Print where things actually resolved, for when an import fails anyway.
doctor:
	@echo "make PY      : $(PY) -> $$($(PY) -c 'import sys;print(sys.executable)')"
	@echo "venv python  : $$($(VPY) -c 'import sys;print(sys.executable)' 2>/dev/null || echo MISSING)"
	@echo "venv prefix  : $$($(VPY) -c 'import sys;print(sys.prefix)' 2>/dev/null || echo MISSING)"
	@echo "VIRTUAL_ENV  : $${VIRTUAL_ENV:-<unset>}"
	@echo "PYTHONPATH   : $${PYTHONPATH:-<unset>}"
	@$(VPY) -c "import yaml,pydantic,rich,anthropic;print('imports  : ok')" 2>&1 | tail -1

clean:
	rm -rf reports $(VENV) .pytest_cache **/__pycache__ *.egg-info
