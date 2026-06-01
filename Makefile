.PHONY: setup test run package

setup:
	bash scripts/setup_workspace.sh

test:
	. .venv/bin/activate && python -m pytest tests/ -q

run:
	. .venv/bin/activate && python main.py

package:
	bash scripts/package_for_vps.sh
