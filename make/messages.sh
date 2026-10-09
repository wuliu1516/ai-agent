# Makefile 的中文提示信息。
#
# Windows 版 GNU Make 会按系统 ANSI 码页（如 GBK）转码配方后再交给 sh，
# 配方里直接写中文会变成乱码甚至语法错误。因此配方只写 ASCII，中文统一
# 放在这里，由 sh 在运行时读取。本文件必须保存为 UTF-8 + LF。
#
# 用法：say <消息键> [参数...]；say_err 同上，但输出到 stderr。

label() {
    case "$1" in
        backend)  printf '后端' ;;
        frontend) printf '前端' ;;
        *)        printf '%s' "$1" ;;
    esac
}

say() {
    key=$1
    shift
    case "$key" in
        # help
        help_title)        printf 'CSpider NL2SQL 工作台\n\n目标：\n' ;;
        help_vars)         printf '\n可覆盖变量：PYTHON, NODE, NPM, CSPIDER_DATA_SOURCE_DIR, BACKEND_PORT, FRONTEND_PORT\n例如：make setup PYTHON=/d/Python312/python.exe\n' ;;

        # setup
        check_python)      printf '==> 检查 Python\n' ;;
        python_ok)         printf '    Python %s\n' "$1" ;;
        err_python)        printf '错误：需要 Python 3.12.4，当前为 '\''%s'\''。可用 make PYTHON=<解释器路径> 指定。\n' "$1" ;;
        venv_create)       printf '==> 创建虚拟环境 %s\n' "$1" ;;
        venv_reuse)        printf '==> 复用现有虚拟环境 %s\n' "$1" ;;
        err_venv_python)   printf '错误：%s 中的 Python 为 '\''%s'\''，需要 3.12.4。请删除 .venv 后重试。\n' "$1" "$2" ;;
        install_backend)   printf '==> 安装后端依赖\n' ;;
        install_frontend)  printf '==> 安装前端依赖\n' ;;
        deps_done)         printf '==> 依赖安装完成。\n' ;;
        check_source)      printf '==> 检查原始数据：%s\n' "$1" ;;
        err_source_dir)    printf '错误：找不到数据目录 '\''%s'\''。请用 CSPIDER_DATA_SOURCE_DIR=<路径> 指定。\n' "$1" ;;
        err_source_file)   printf '错误：数据不完整，缺少 %s\n' "$1" ;;
        split_data)        printf '==> 生成数据划分到 %s\n' "$1" ;;
        setup_done)        printf '==> 环境准备完成。启动：make start\n' ;;

        # checks
        err_no_file)       printf '错误：未找到 %s。请先运行 make setup。\n' "$1" ;;
        err_node)          printf '错误：需要 Node.js 24.x，当前为 '\''%s'\''。\n' "$1" ;;
        err_npm)           printf '错误：需要 npm 11.x，当前为 '\''%s'\''。\n' "$1" ;;

        # start
        starting)          printf '==> 启动%s（端口 %s）\n' "$(label "$1")" "$2" ;;
        already_running)   printf '    %s已在运行（PID %s）：%s\n' "$(label "$1")" "$2" "$3" ;;
        err_port_busy)     printf '错误：端口 %s 已被 PID %s 占用，但它不是健康的工作台后端。\n' "$1" "$2" ;;
        launched)          printf '    已启动，日志：%s\n' "$1" ;;
        waiting)           printf '==> 等待服务就绪\n' ;;
        err_not_ready)     printf '错误：服务未能在 30 秒内就绪。请检查 %s 与 %s。\n' "$1" "$2" ;;
        ready)             printf '\n工作台已启动：%s\n后端接口文档：%s\n' "$1" "$2" ;;

        # stop
        stopped)           printf '%s：已停止监听进程 PID %s（端口 %s）。\n' "$(label "$1")" "$2" "$3" ;;
        err_stop)          printf '%s：无法停止 PID %s（端口 %s）。\n' "$(label "$1")" "$2" "$3" ;;
        not_listening)     printf '%s：端口 %s 上没有监听进程。\n' "$(label "$1")" "$2" ;;

        # status
        status_up)         printf '%s：  运行中（PID %s，%s）\n' "$(label "$1")" "$2" "$3" ;;
        status_down)       printf '%s：  不可用\n' "$(label "$1")" ;;

        # clean
        cleaned)           printf '已清理 PID 文件与日志。\n' ;;
        distcleaned)       printf '已删除 .venv、node_modules、%s 与 build。\n' "$1" ;;

        *)                 printf '[%s] %s\n' "$key" "$*" ;;
    esac
}

say_err() {
    say "$@" >&2
}
