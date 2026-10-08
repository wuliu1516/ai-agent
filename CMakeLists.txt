cmake_minimum_required(VERSION 4.4)

project(CSpiderNL2SQLWorkbench LANGUAGES NONE)

# CMake drives the reproducible Windows development workflow:
#   cmake -S . -B build
#   cmake --build build --target setup
#   cmake --build build --target start
# No compiler is required; CMake is used as the task runner.

set(CSPIDER_DATA_SOURCE_DIR "D:/dataset/CSpider" CACHE PATH
    "Directory containing the original CSpider files and database directory")
set(CSPIDER_PYTHON_EXECUTABLE "" CACHE FILEPATH
    "Python 3.12.4 interpreter used to create the project's virtual environment")
set(CSPIDER_NODE_EXECUTABLE "" CACHE FILEPATH
    "Node.js 24.x executable used for the Vite frontend")
set(CSPIDER_NPM_EXECUTABLE "" CACHE FILEPATH
    "npm command matching CSPIDER_NODE_EXECUTABLE")

function(cspider_fail_if_failed description result)
    if(NOT "${${result}}" STREQUAL "0")
        message(FATAL_ERROR "${description} failed with exit code ${${result}}.")
    endif()
endfunction()

# Prefer the exact Python installed by the documented bootstrap procedure instead
# of a Microsoft Store shim or another Python installation on PATH.
if(NOT CSPIDER_PYTHON_EXECUTABLE)
    if(WIN32 AND EXISTS "$ENV{LOCALAPPDATA}/Programs/Python/Python312/python.exe")
        set(CSPIDER_PYTHON_EXECUTABLE
            "$ENV{LOCALAPPDATA}/Programs/Python/Python312/python.exe")
    else()
        find_program(CSPIDER_PYTHON_EXECUTABLE NAMES python3.12 python)
    endif()
endif()
if(NOT EXISTS "${CSPIDER_PYTHON_EXECUTABLE}")
    message(FATAL_ERROR
        "Python 3.12.4 was not found. Set CSPIDER_PYTHON_EXECUTABLE or install it first.")
endif()
execute_process(
    COMMAND "${CSPIDER_PYTHON_EXECUTABLE}" -c "import platform; print(platform.python_version())"
    OUTPUT_VARIABLE _python_version
    OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE _python_version_result
)
cspider_fail_if_failed("Checking Python" _python_version_result)
if(NOT _python_version STREQUAL "3.12.4")
    message(FATAL_ERROR
        "Python version mismatch: found ${_python_version}; this project requires 3.12.4.")
endif()

# Winget's portable Node package can be installed in the user package directory
# before PATH is refreshed. Locate its npm.cmd first so the Node and npm binaries
# always come from the same Node distribution.
if(NOT CSPIDER_NPM_EXECUTABLE)
    find_program(CSPIDER_NPM_EXECUTABLE NAMES npm.cmd npm)
endif()
if(NOT CSPIDER_NPM_EXECUTABLE AND WIN32 AND DEFINED ENV{LOCALAPPDATA})
    file(GLOB _winget_npm_candidates
        "$ENV{LOCALAPPDATA}/Microsoft/WinGet/Packages/OpenJS.NodeJS_*/*/npm.cmd")
    list(SORT _winget_npm_candidates COMPARE NATURAL ORDER DESCENDING)
    list(LENGTH _winget_npm_candidates _winget_npm_count)
    if(_winget_npm_count GREATER 0)
        list(GET _winget_npm_candidates 0 CSPIDER_NPM_EXECUTABLE)
    endif()
endif()
if(NOT EXISTS "${CSPIDER_NPM_EXECUTABLE}")
    message(FATAL_ERROR
        "npm was not found. Install Node.js 24.x or set CSPIDER_NPM_EXECUTABLE.")
endif()

if(NOT CSPIDER_NODE_EXECUTABLE)
    get_filename_component(_npm_directory "${CSPIDER_NPM_EXECUTABLE}" DIRECTORY)
    if(WIN32 AND EXISTS "${_npm_directory}/node.exe")
        set(CSPIDER_NODE_EXECUTABLE "${_npm_directory}/node.exe")
    else()
        find_program(CSPIDER_NODE_EXECUTABLE NAMES node)
    endif()
endif()
if(NOT EXISTS "${CSPIDER_NODE_EXECUTABLE}")
    message(FATAL_ERROR
        "Node.js was not found. Set CSPIDER_NODE_EXECUTABLE to the Node executable matching npm.")
endif()
execute_process(
    COMMAND "${CSPIDER_NODE_EXECUTABLE}" --version
    OUTPUT_VARIABLE _node_version
    OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE _node_version_result
)
cspider_fail_if_failed("Checking Node.js" _node_version_result)
if(NOT _node_version MATCHES "^v24\\.")
    message(FATAL_ERROR
        "Node.js version mismatch: found ${_node_version}; this project requires Node.js 24.x.")
endif()
# CMake 4.x invokes .cmd launchers through the Windows command processor.
execute_process(
    COMMAND "${CSPIDER_NPM_EXECUTABLE}" --version
    OUTPUT_VARIABLE _npm_version
    OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE _npm_version_result
)
cspider_fail_if_failed("Checking npm" _npm_version_result)
if(NOT _npm_version MATCHES "^11\\.")
    message(FATAL_ERROR
        "npm version mismatch: found ${_npm_version}; this project requires npm 11.x.")
endif()

set(CSPIDER_VENV_PYTHON "${CMAKE_SOURCE_DIR}/.venv/Scripts/python.exe")
set(CSPIDER_VITE_ENTRY "${CMAKE_SOURCE_DIR}/frontend/node_modules/vite/bin/vite.js")

function(add_cspider_script_target target script)
    add_custom_target(${target}
        COMMAND "${CMAKE_COMMAND}"
            "-DPROJECT_ROOT:PATH=${CMAKE_SOURCE_DIR}"
            "-DPYTHON_EXECUTABLE:FILEPATH=${CSPIDER_PYTHON_EXECUTABLE}"
            "-DNODE_EXECUTABLE:FILEPATH=${CSPIDER_NODE_EXECUTABLE}"
            "-DNPM_EXECUTABLE:FILEPATH=${CSPIDER_NPM_EXECUTABLE}"
            "-DDATA_SOURCE_DIR:PATH=${CSPIDER_DATA_SOURCE_DIR}"
            -P "${CMAKE_SOURCE_DIR}/cmake/${script}"
        WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
        USES_TERMINAL
        VERBATIM
    )
endfunction()

add_cspider_script_target(setup SetupEnvironment.cmake)
add_cspider_script_target(setup-deps SetupDependencies.cmake)
add_cspider_script_target(start StartServices.cmake)
add_cspider_script_target(stop StopServices.cmake)
add_cspider_script_target(status StatusServices.cmake)
add_custom_target(run DEPENDS start)

message(STATUS "CSpider task runner configured")
message(STATUS "  Python: ${CSPIDER_PYTHON_EXECUTABLE} (${_python_version})")
message(STATUS "  Node:   ${CSPIDER_NODE_EXECUTABLE} (${_node_version})")
message(STATUS "  npm:    ${CSPIDER_NPM_EXECUTABLE} (${_npm_version})")
message(STATUS "  Data:   ${CSPIDER_DATA_SOURCE_DIR}")
message(STATUS "Targets: setup, setup-deps, start (or run), stop, status")

