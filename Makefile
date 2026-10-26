### Defensive settings for make:
#     https://tech.davis-hansson.com/p/make/
SHELL:=bash
.SHELLFLAGS:=-eu -o pipefail -O inherit_errexit -c
.SILENT:
.DELETE_ON_ERROR:
MAKEFLAGS+=--warn-undefined-variables
MAKEFLAGS+=--no-builtin-rules

GREEN=`tput setaf 2`
RESET=`tput sgr0`

UV?=uv

ifeq (, $(shell which $(UV) ))
  $(error "UV=$(UV) not found in $(PATH)")
endif

VENV_FOLDER=.venv
BIN_FOLDER=$(VENV_FOLDER)/bin

.PHONY: all
all: install

.PHONY: help
help: ## This help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-24s\033[0m %s\n", $$1, $$2}'

$(VENV_FOLDER): ## Create the virtual environment
	@echo "$(GREEN)==> Installing$(RESET)"
	@uv sync --group test

.PHONY: install
install: $(VENV_FOLDER) ## Install the package and its test dependencies

.PHONY: install-plone
install-plone: ## Install with Plone, for the tests that need a real site
	@echo "$(GREEN)==> Installing with Plone$(RESET)"
	@uv sync --inexact --extra plone --extra testing --group test --group test-plone

.PHONY: install-bench
install-bench: ## Install the benchmark harness (adds a2wsgi)
	@echo "$(GREEN)==> Installing benchmark dependencies$(RESET)"
	@uv sync --inexact --group bench --group test

.PHONY: clean
clean: ## Remove the virtual environment and build artifacts
	@echo "$(GREEN)==> Cleaning$(RESET)"
	@rm -rf $(VENV_FOLDER) $(NO_PLONE_VENV) dist build .pytest_cache .ruff_cache

.PHONY: format
format: $(VENV_FOLDER) ## Format the codebase
	@echo "$(GREEN)==> Formatting$(RESET)"
	@uvx ruff check --fix .
	@uvx ruff format .

# --no-fix because the ruff config sets fix = true, which would make this target
# rewrite the tree instead of reporting on it. CI has to be read-only.
.PHONY: lint
lint: $(VENV_FOLDER) ## Check the codebase without modifying it
	@echo "$(GREEN)==> Linting$(RESET)"
	@uvx ruff check --no-fix .
	@uvx ruff format --check .

.PHONY: check
check: format lint ## Format then check

.PHONY: test
test: $(VENV_FOLDER) ## Run the tests
	@echo "$(GREEN)==> Running tests$(RESET)"
	@uv run pytest

# A dedicated environment rather than a prune of .venv. `uv sync` uninstalls the
# packages but leaves behind directories that were not listed in a wheel's
# RECORD, and a single orphaned Products/MimetypesRegistry/mime_types is enough
# to keep `Products` importable as a namespace package — which fails the
# assertion below for a reason that has nothing to do with Plone being present.
# It also stops this target and test-plone from reinstalling ~200 packages over
# each other on every alternation.
NO_PLONE_VENV=.venv-no-plone
NO_PLONE=UV_PROJECT_ENVIRONMENT=$(NO_PLONE_VENV)

.PHONY: test-no-plone
test-no-plone: ## Assert Plone is absent, then run the suite
	@echo "$(GREEN)==> Running tests in a Plone-free environment$(RESET)"
	@$(NO_PLONE) uv sync --group test
	@$(NO_PLONE) uv run python -c "import importlib.util as u; \
	  present = [p for p in ('Products', 'Zope2', 'AccessControl', 'plone') \
	             if u.find_spec(p)]; \
	  assert not present, f'Plone is installed: {present}'; \
	  print('  Products, Zope2, AccessControl, plone: all absent')"
	@$(NO_PLONE) uv run pytest

# The mirror of test-no-plone. tests/conftest.py skips tests/plone entirely when
# Plone is absent, which is what keeps `make test` green in a Zope-free venv — so
# without this assertion the Plone half could silently cover nothing and still
# report success. That is the exact failure mode this package cannot afford.
.PHONY: test-plone
test-plone: install-plone ## Assert Plone is present, then run the tests that need it
	@echo "$(GREEN)==> Running the Plone half of the suite$(RESET)"
	@uv run python -c "import importlib.util as u; \
	  missing = [p for p in ('Products', 'Zope2', 'AccessControl', 'plone', \
	                         'plone.app.testing', 'pytest_plone') \
	             if not u.find_spec(p)]; \
	  assert not missing, f'not installed: {missing}'; \
	  print('  Plone and the test harness: present')"
	@uv run pytest tests/plone

.PHONY: build
build: ## Build the sdist and wheel
	@echo "$(GREEN)==> Building$(RESET)"
	@uv build

# Inside the project env, not uvx: towncrier imports the package to read its
# version, and an isolated tool env cannot see it.
.PHONY: changelog
changelog: $(VENV_FOLDER) ## Preview the changelog
	@uv run --with towncrier towncrier build --draft

# The documentation is its own uv project, with its own environment in
# docs/.venv: Sphinx and its extensions stay out of the package's test env, and
# docs/Makefile is the cookieplone scaffold's, kept close to upstream.
.PHONY: docs-install
docs-install: ## Install the documentation toolchain
	@$(MAKE) -C docs install

.PHONY: docs-html
docs-html: ## Build the documentation as HTML, failing on any warning
	@$(MAKE) -C docs html SPHINXOPTS="-W --keep-going"

.PHONY: docs-livehtml
docs-livehtml: ## Rebuild the documentation on every change and serve it
	@$(MAKE) -C docs livehtml

.PHONY: docs-vale
docs-vale: ## Check the documentation's spelling, grammar and style
	@$(MAKE) -C docs vale VALEOPTS="--minAlertLevel='warning'"

.PHONY: docs-linkcheck
docs-linkcheck: ## Check the documentation for broken links
	@$(MAKE) -C docs linkcheckbroken

ADAPTER?=ploneasgi
SLOW_CLIENTS?=16
REPS?=3

# Serves; it does not measure. For the two-shell workflow, or to point ab/hey at
# it. `bench-starve` and friends below start and stop this for you.
.PHONY: bench-serve
bench-serve: install-bench ## Serve the bench app under ADAPTER=ploneasgi|a2wsgi
	@echo "$(GREEN)==> Serving under $(ADAPTER) on 127.0.0.1:8091$(RESET)"
	@uv run python benchmarks/bench_adapter.py $(ADAPTER) --port 8091 --stream-bytes 52428800

.PHONY: bench-starve
bench-starve: install-bench ## Page availability under SLOW_CLIENTS slow downloads
	@echo "$(GREEN)==> Starvation: $(ADAPTER), $(SLOW_CLIENTS) slow clients$(RESET)"
	@uv run python benchmarks/run.py starve --adapter $(ADAPTER) --slow-clients $(SLOW_CLIENTS)

.PHONY: bench-throughput
bench-throughput: install-bench ## Full-speed download rate, REPS repetitions
	@echo "$(GREEN)==> Throughput: $(ADAPTER), $(REPS) reps$(RESET)"
	@uv run python benchmarks/run.py throughput --adapter $(ADAPTER) --reps $(REPS)

.PHONY: bench-capacity
bench-capacity: install-bench ## Sustained page req/s with downloads in flight
	@echo "$(GREEN)==> Capacity: $(ADAPTER), $(SLOW_CLIENTS) slow clients$(RESET)"
	@uv run python benchmarks/run.py capacity --adapter $(ADAPTER) --slow-clients $(SLOW_CLIENTS)

# Alternates the arms rather than running all of one and then all of the other:
# the machine drifts between blocks, and that drift lands on the comparison.
# Each cycle is a fresh server per arm, which is the only honest way to A/B two
# adapters that are chosen at mount time.
.PHONY: bench-compare
bench-compare: install-bench ## Interleave ploneasgi and a2wsgi, REPS cycles
	@echo "$(GREEN)==> Interleaved A/B over $(REPS) cycles$(RESET)"
	@for i in $$(seq 1 $(REPS)); do \
	  echo "--- cycle $$i ---"; \
	  for arm in ploneasgi a2wsgi; do \
	    uv run python benchmarks/run.py throughput --adapter $$arm --reps 1; \
	  done; \
	done
	@echo "$(GREEN)Read the spread, not the mean of one cycle.$(RESET)"

# The categorical claim, as a gate. Deselected from `make test` by the `bench`
# marker because each case spends tens of seconds waiting on real sockets.
.PHONY: bench-test
bench-test: install-bench ## Run the benchmark assertions
	@echo "$(GREEN)==> Benchmark gate$(RESET)"
	@uv run python -c "import importlib.util as u; \
	  assert u.find_spec('a2wsgi'), 'a2wsgi missing: the falsifier arm cannot run'; \
	  print('  a2wsgi present: the comparison is falsifiable')"
	@uv run pytest tests/bench -m bench

# The same claim with Zope as the app, plus the checks that need a running
# instance. Plone and a2wsgi both have to be present: one is the app, the other
# the falsifier.
.PHONY: bench-plone
bench-plone: install-plone install-bench ## Run the starvation gate and live checks against real Plone
	@echo "$(GREEN)==> Plone-backed benchmark gate$(RESET)"
	@uv run python -c "import importlib.util as u; \
	  missing = [p for p in ('Products.CMFPlone', 'a2wsgi') if not u.find_spec(p)]; \
	  assert not missing, f'not installed: {missing}'; \
	  print('  Plone and a2wsgi: present')"
	@uv run pytest tests/bench -m bench_plone

.PHONY: install-storage
install-storage: ## Install the networked storages (ZEO, RelStorage, PGJsonb)
	@echo "$(GREEN)==> Installing storage backends$(RESET)"
	@uv sync --inexact --group bench-storage --group test

# The same gate on every networked storage, each of which fetches a blob into a
# local cache on first read. PostgreSQL comes from a throwaway Docker container,
# or from BENCH_PG_DSN when that names a server.
.PHONY: bench-storage
bench-storage: install-plone install-bench install-storage ## Run the Plone gate on ZEO, RelStorage and PGJsonb
	@echo "$(GREEN)==> Storage matrix$(RESET)"
	@uv run python -c "import importlib.util as u; \
	  missing = [p for p in ('Products.CMFPlone', 'a2wsgi', 'ZEO', 'relstorage', \
	                         'psycopg2', 'zodb_pgjsonb') if not u.find_spec(p)]; \
	  assert not missing, f'not installed: {missing}'; \
	  print('  Plone, a2wsgi and the three storages: present')"
	@uv run pytest tests/bench -m bench_storage
