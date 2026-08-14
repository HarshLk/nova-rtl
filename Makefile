PYTHON ?= python

.PHONY: setup install test lint check doctor clean

setup:
	conda env create --file environment.yml

install:
	$(PYTHON) -m pip install --requirement requirements.txt

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .

check: lint test

doctor:
	nova doctor

clean:
	$(PYTHON) -c "from pathlib import Path; import shutil; [shutil.rmtree(path) for path in (Path('.pytest_cache'), Path('.ruff_cache'), Path('build'), Path('dist')) if path.exists()]"
