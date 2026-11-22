# One-command workflows for the team. Requires: uv, Rust (cargo), Python 3.12 (uv installs it).
RUN ?= mock1
CONFIG ?= configs/mock.yaml
PY := .venv/bin/python

.PHONY: setup test test-rust test-py smoke run viewer serve gateways clean

setup:                ## create .venv from uv.lock, build the Rust extension + gateway
	uv sync --python 3.12 --extra dev --extra fast
	cargo build --release -p rsi-gateway

test: test-rust test-py

test-rust:
	cargo test -p rsi-core --no-default-features
	cargo test -p rsi-gateway

test-py:
	$(PY) -m pytest -q

smoke:                ## one self-play game (mock by default; CONFIG=configs/api.yaml for the real API)
	$(PY) -m rsi.loop smoke --config $(CONFIG) --run-name $(RUN)-smoke

run:                  ## full RSI loop
	$(PY) -m rsi.loop run --config $(CONFIG) --run-name $(RUN)

viewer:               ## static pages: runs/$(RUN)/index.html
	$(PY) -m rsi.viewer build runs/$(RUN)

serve:                ## live viewer + gateway telemetry on http://127.0.0.1:8765
	$(PY) -m rsi.viewer serve runs/$(RUN) --gateway player=http://127.0.0.1:9000 --gateway reviewer=http://127.0.0.1:9001

gateways:             ## API gateways on :9000 (player) and :9001 (reviewer); needs .env
	scripts/gateways.sh

clean:
	rm -rf target .venv .pytest_cache
