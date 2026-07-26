.PHONY: setup chat test eval demo

setup:
	pip install -e .

chat:
	python -m helpdesk.cli chat

test:
	pytest -q

eval:
	python eval/run_eval.py

demo:
	@echo "See docs/demo_script.md (produced on D5)"
