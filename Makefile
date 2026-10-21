# Developer shortcuts. The console is plain Python + a static web UI; nothing here is needed to run it.
PY ?= python3

.PHONY: install web demo dev test test-all a11y lock docker

install:          ## the console and the host agent, with test tools
	$(PY) -m pip install -e ".[console,dev]"
	cd web && npm ci

web:              ## build the web UI into the Python package (raiden/console/static)
	cd web && npm run build
	rm -rf raiden/console/static && cp -R web/dist raiden/console/static

demo: web         ## the console on a simulated fleet with a scripted model: http://127.0.0.1:8787
	RAIDEN_CONSOLE_DATA=$${TMPDIR:-/tmp}/raiden-demo raiden console --demo --port 8787

dev:              ## hot-reloading UI on :5173 against a demo console on :8787 (run `make demo` in another shell)
	cd web && npm run dev

test:             ## unit and API tests (no PostgreSQL, no AI endpoint needed)
	$(PY) -m pytest

test-all:         ## plus the two-minute end-to-end incident on the demo fleet
	RAIDEN_SLOW_TESTS=1 $(PY) -m pytest

a11y:             ## WCAG 2.1 AA check (axe-core) of every page in both themes, against `make demo` (needs Google Chrome)
	cd web && npm install --no-save --no-audit --no-fund puppeteer-core@25.13.0 axe-core@4.14.0 && node scripts/a11y.mjs

lock:             ## re-pin the console's dependencies with hashes after changing pyproject.toml (needs uv; a mirror: UV_INDEX_URL)
	uv pip compile pyproject.toml --extra console --universal --python-version 3.12 --no-header --quiet --generate-hashes -o requirements/console.lock
	sed -i.bak '/--hash=md5:/d' requirements/console.lock && rm requirements/console.lock.bak   # some mirrors list md5 too; pip refuses it

docker:
	docker build -t raiden-console:0.2.0 .
