.PHONY: setup chat resume test eval demo

# 优先用项目 venv 的解释器;不存在时回落 PATH 上的 python3
PY := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

setup:
	$(PY) -m pip install --upgrade pip  # 系统自带 pip 过旧,不支持 pyproject 可编辑安装
	$(PY) -m pip install -e .

# 用法:make chat ARGS="--as-user u-alice"
chat:
	$(PY) -m helpdesk.cli chat $(ARGS)

# 用法:make resume CASE=case-xxxx ARGS="--as-user u-alice"
resume:
	$(PY) -m helpdesk.cli resume $(CASE) $(ARGS)

test:
	$(PY) -m pytest -q

eval:
	$(PY) eval/run_eval.py

demo:
	@echo "See docs/demo_script.md (produced on D5)"
