# CSpider NL2SQL 工作台 —— 任务运行器
#
# 依赖：Git for Windows（提供 sh.exe）和 GNU Make。
#   winget install ezwinports.make
#
# 用法：在仓库根目录执行 `make`，或 `make <目标>`。
#
# 注意：配方（以 Tab 开头的行）中不要写中文。Windows 版 make 会按 ANSI 码页
# 转码配方后交给 sh，中文会变成乱码甚至语法错误。提示文字统一放在
# make/messages.sh，通过 say / say_err 输出。

.DEFAULT_GOAL := help
.NOTPARALLEL:

# ---------------------------------------------------------------------------
# 可覆盖变量：make setup PYTHON=/path/to/python.exe
# ---------------------------------------------------------------------------
PYTHON ?= python
NODE   ?= node
NPM    ?= npm

# CSpider 原始数据目录（含 train.json / dev.json / tables.json / database 等）。
CSPIDER_DATA_SOURCE_DIR ?= D:/dataset/CSpider

BACKEND_HOST  ?= 127.0.0.1
BACKEND_PORT  ?= 8000
FRONTEND_HOST ?= 127.0.0.1
FRONTEND_PORT ?= 5173

# ---------------------------------------------------------------------------
# 固定路径
# ---------------------------------------------------------------------------
ROOT         := $(CURDIR)
VENV         := .venv
VENV_PYTHON  := $(VENV)/Scripts/python.exe
FRONTEND_DIR := frontend
VITE_ENTRY   := node_modules/vite/bin/vite.js
DATA_ROOT    := data/CSpider

BACKEND_LOG  := backend/server.log
BACKEND_ERR  := backend/server-error.log
BACKEND_PID  := backend/server.pid
FRONTEND_LOG := frontend/vite.log
FRONTEND_ERR := frontend/vite-error.log
FRONTEND_PID := frontend/server.pid

BACKEND_URL  := http://$(BACKEND_HOST):$(BACKEND_PORT)
FRONTEND_URL := http://$(FRONTEND_HOST):$(FRONTEND_PORT)
HEALTH_URL   := $(BACKEND_URL)/api/health

SOURCE_FILES         := train.json train_gold.sql dev.json dev_gold.sql \
                        tables.json char_emb.txt README.txt
REQUIRED_SPLIT_FILES := development.json development_gold.sql \
                        validation.json validation_gold.sql \
                        test.json test_gold.sql tables.json

# 每条配方行前加 $(MSG)，即可使用 say / say_err。
MSG := . "$(ROOT)/make/messages.sh";

# 配方依赖 POSIX shell。普通 PowerShell 的 PATH 里通常没有 sh.exe，此时 make
# 会静默改用 cmd.exe，因此这里通过 git 定位 Git for Windows 自带的 sh.exe，
# 并把它的 usr/bin（nohup、grep、awk 等）加入 PATH。
ifeq ($(OS),Windows_NT)
GIT_EXEC_PATH := $(firstword $(shell git --exec-path))
GIT_SH        := $(wildcard $(GIT_EXEC_PATH)/../../../usr/bin/sh.exe)
ifeq ($(GIT_SH),)
$(error 未找到 Git for Windows 自带的 sh.exe。请安装 Git for Windows 并确保 git 在 PATH 中)
endif
GIT_USR_BIN := $(abspath $(dir $(GIT_SH)))
SHELL       := $(GIT_USR_BIN)/sh.exe
export PATH := $(subst /,\,$(GIT_USR_BIN));$(PATH)
endif

# 按端口查找监听进程的 PID。
find_pid = netstat -ano | grep -i LISTENING | grep -E ":$(1)[[:space:]]" | awk '{print $$NF}' | tr -d '\r' | head -1

# 后端健康检查。
health_ok = curl -sf -o /dev/null --max-time 2 "$(HEALTH_URL)"

# ---------------------------------------------------------------------------
.PHONY: help
help: ## 显示本帮助
	@$(MSG) say help_title
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@$(MSG) say help_vars

# ---------------------------------------------------------------------------
.PHONY: setup
setup: setup-deps split-data ## 首次准备：创建 venv、安装依赖并生成数据划分
	@$(MSG) say setup_done

.PHONY: setup-deps
setup-deps: ## 只创建 venv 并安装 Python / 前端依赖
	@$(MSG) say check_python; \
	v=$$("$(PYTHON)" -c 'import platform; print(platform.python_version())' 2>/dev/null); \
	if [ "$$v" != "3.12.4" ]; then say_err err_python "$$v"; exit 1; fi; \
	say python_ok "$$v"
	@$(MSG) if [ ! -f "$(VENV_PYTHON)" ]; then \
		say venv_create "$(VENV)"; \
		"$(PYTHON)" -m venv "$(VENV)" || exit 1; \
	else \
		say venv_reuse "$(VENV)"; \
	fi; \
	v=$$("$(VENV_PYTHON)" -c 'import platform; print(platform.python_version())' 2>/dev/null); \
	if [ "$$v" != "3.12.4" ]; then say_err err_venv_python "$(VENV)" "$$v"; exit 1; fi
	@$(MSG) say install_backend
	@"$(VENV_PYTHON)" -m pip install --disable-pip-version-check -r backend/requirements.txt
	@"$(VENV_PYTHON)" -m pip check
	@$(MSG) say install_frontend
	@cd "$(FRONTEND_DIR)" && "$(NPM)" ci
	@$(MSG) say deps_done

.PHONY: split-data
split-data: ## 从原始 CSpider 数据生成 development / validation / test 划分
	@$(MSG) src="$(CSPIDER_DATA_SOURCE_DIR)"; \
	say check_source "$$src"; \
	if [ ! -d "$$src" ]; then say_err err_source_dir "$$src"; exit 1; fi; \
	for f in $(SOURCE_FILES); do \
		if [ ! -f "$$src/$$f" ]; then say_err err_source_file "$$src/$$f"; exit 1; fi; \
	done; \
	if [ ! -d "$$src/database" ]; then say_err err_source_file "$$src/database"; exit 1; fi; \
	if [ ! -f "$(VENV_PYTHON)" ]; then say_err err_no_file "$(VENV_PYTHON)"; exit 1; fi; \
	say split_data "$(DATA_ROOT)"
	@CSPIDER_SOURCE_DIR="$(CSPIDER_DATA_SOURCE_DIR)" "$(VENV_PYTHON)" split_cspider.py

# ---------------------------------------------------------------------------
.PHONY: start
start: check-runtime check-data ## 后台启动后端与前端，并等待健康检查通过
	@$(MSG) say starting backend $(BACKEND_PORT); \
	pid=$$($(call find_pid,$(BACKEND_PORT))); \
	if [ -n "$$pid" ]; then \
		if $(health_ok); then \
			say already_running backend "$$pid" "$(BACKEND_URL)"; \
		else \
			say_err err_port_busy $(BACKEND_PORT) "$$pid"; exit 1; \
		fi; \
	else \
		: > "$(BACKEND_LOG)"; : > "$(BACKEND_ERR)"; \
		nohup "$(VENV_PYTHON)" -m uvicorn backend.main:app \
			--host "$(BACKEND_HOST)" --port "$(BACKEND_PORT)" \
			> "$(BACKEND_LOG)" 2> "$(BACKEND_ERR)" < /dev/null & \
		say launched "$(BACKEND_LOG)"; \
	fi
	@$(MSG) say starting frontend $(FRONTEND_PORT); \
	pid=$$($(call find_pid,$(FRONTEND_PORT))); \
	if [ -n "$$pid" ]; then \
		say already_running frontend "$$pid" "$(FRONTEND_URL)"; \
	else \
		: > "$(FRONTEND_LOG)"; : > "$(FRONTEND_ERR)"; \
		( cd "$(FRONTEND_DIR)" && \
		  nohup "$(NODE)" "$(VITE_ENTRY)" \
			--host "$(FRONTEND_HOST)" --port "$(FRONTEND_PORT)" --strictPort \
			> "$(ROOT)/$(FRONTEND_LOG)" 2> "$(ROOT)/$(FRONTEND_ERR)" < /dev/null & ); \
		say launched "$(FRONTEND_LOG)"; \
	fi
	@$(MSG) say waiting; \
	i=0; ok=0; \
	while [ $$i -lt 60 ]; do \
		if $(health_ok) && [ -n "$$($(call find_pid,$(FRONTEND_PORT)))" ]; then ok=1; break; fi; \
		i=$$((i+1)); sleep 0.5; \
	done; \
	if [ $$ok -ne 1 ]; then say_err err_not_ready "$(BACKEND_ERR)" "$(FRONTEND_ERR)"; exit 1; fi; \
	$(call find_pid,$(BACKEND_PORT)) > "$(BACKEND_PID)"; \
	$(call find_pid,$(FRONTEND_PORT)) > "$(FRONTEND_PID)"; \
	say ready "$(FRONTEND_URL)" "$(BACKEND_URL)/docs"

# ---------------------------------------------------------------------------
.PHONY: stop
stop: ## 停止本工作台在 5173 / 8000 上的监听服务
	@$(MSG) for entry in "frontend:$(FRONTEND_PORT):$(FRONTEND_PID)" "backend:$(BACKEND_PORT):$(BACKEND_PID)"; do \
		name=$${entry%%:*}; rest=$${entry#*:}; port=$${rest%%:*}; pidfile=$${rest#*:}; \
		pid=$$($(call find_pid,$$port)); \
		if [ -n "$$pid" ]; then \
			if taskkill //F //T //PID "$$pid" > /dev/null 2>&1; then \
				say stopped "$$name" "$$pid" "$$port"; \
			else \
				say_err err_stop "$$name" "$$pid" "$$port"; \
			fi; \
		else \
			say not_listening "$$name" "$$port"; \
		fi; \
		rm -f "$$pidfile"; \
	done

.PHONY: restart
restart: stop start ## 重启后端与前端

# ---------------------------------------------------------------------------
.PHONY: status
status: ## 查看前后端运行状态（任一不可用则返回非零）
	@$(MSG) healthy=1; \
	pid=$$($(call find_pid,$(BACKEND_PORT))); \
	if [ -n "$$pid" ] && $(health_ok); then \
		say status_up backend "$$pid" "$(BACKEND_URL)"; \
	else \
		say_err status_down backend; healthy=0; \
	fi; \
	pid=$$($(call find_pid,$(FRONTEND_PORT))); \
	if [ -n "$$pid" ]; then \
		say status_up frontend "$$pid" "$(FRONTEND_URL)"; \
	else \
		say_err status_down frontend; healthy=0; \
	fi; \
	[ $$healthy -eq 1 ]

.PHONY: logs
logs: ## 跟踪后端与前端日志（Ctrl-C 退出）
	@touch "$(BACKEND_LOG)" "$(BACKEND_ERR)" "$(FRONTEND_LOG)" "$(FRONTEND_ERR)"
	@tail -n 40 -f "$(BACKEND_LOG)" "$(BACKEND_ERR)" "$(FRONTEND_LOG)" "$(FRONTEND_ERR)"

# ---------------------------------------------------------------------------
.PHONY: run-backend
run-backend: check-runtime check-data ## 前台运行后端（Ctrl-C 退出）
	@"$(VENV_PYTHON)" -m uvicorn backend.main:app --host "$(BACKEND_HOST)" --port "$(BACKEND_PORT)"

.PHONY: run-frontend
run-frontend: check-runtime ## 前台运行前端（Ctrl-C 退出）
	@cd "$(FRONTEND_DIR)" && "$(NODE)" "$(VITE_ENTRY)" --host "$(FRONTEND_HOST)" --port "$(FRONTEND_PORT)" --strictPort

# ---------------------------------------------------------------------------
.PHONY: check-runtime
check-runtime: ## 校验 venv、前端依赖与 Node/npm 版本
	@$(MSG) \
	if [ ! -f "$(VENV_PYTHON)" ]; then say_err err_no_file "$(VENV_PYTHON)"; exit 1; fi; \
	if [ ! -f "$(FRONTEND_DIR)/$(VITE_ENTRY)" ]; then say_err err_no_file "$(FRONTEND_DIR)/$(VITE_ENTRY)"; exit 1; fi; \
	v=$$("$(NODE)" --version 2>/dev/null); \
	case "$$v" in v24.*) ;; *) say_err err_node "$$v"; exit 1 ;; esac; \
	v=$$("$(NPM)" --version 2>/dev/null); \
	case "$$v" in 11.*) ;; *) say_err err_npm "$$v"; exit 1 ;; esac

.PHONY: check-data
check-data: ## 校验 data/CSpider 划分是否就绪
	@$(MSG) \
	for f in $(REQUIRED_SPLIT_FILES); do \
		if [ ! -f "$(DATA_ROOT)/$$f" ]; then say_err err_no_file "$(DATA_ROOT)/$$f"; exit 1; fi; \
	done; \
	if [ ! -d "$(DATA_ROOT)/database" ]; then say_err err_no_file "$(DATA_ROOT)/database"; exit 1; fi

# ---------------------------------------------------------------------------
.PHONY: clean
clean: ## 删除 PID 文件与日志
	@rm -f "$(BACKEND_PID)" "$(FRONTEND_PID)" \
		"$(BACKEND_LOG)" "$(BACKEND_ERR)" "$(FRONTEND_LOG)" "$(FRONTEND_ERR)"
	@$(MSG) say cleaned

.PHONY: distclean
distclean: clean ## 在 clean 基础上删除 .venv、node_modules、data 与 build
	@rm -rf "$(VENV)" "$(FRONTEND_DIR)/node_modules" "$(DATA_ROOT)" build
	@$(MSG) say distcleaned "$(DATA_ROOT)"
