.PHONY: help install check test lint fmt dashboard backtest drill report status clean

PY := python
export PYTHONPATH := src

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## install the package with dev dependencies
	$(PY) -m pip install -e ".[dev]"

check:  ## validate configuration
	$(PY) -m tradebot check

test:  ## run the full test suite
	$(PY) -m pytest

lint:  ## ruff + mypy
	ruff check src tests
	mypy src

fmt:  ## auto-fix lint issues
	ruff check --fix src tests

dashboard:  ## bot + dashboard on http://localhost:8000
	$(PY) -m tradebot dashboard --speed 600

backtest:  ## 10 day synthetic backtest with validation gates
	$(PY) -m tradebot backtest --days 10

drill:  ## kill-switch fire drill
	$(PY) -m tradebot drill

report:  ## build today's report now
	$(PY) -m tradebot report

status:  ## positions, kill switch, recent events
	$(PY) -m tradebot status

clean:  ## remove local state (not the config)
	rm -rf data reports logs .pytest_cache .ruff_cache
