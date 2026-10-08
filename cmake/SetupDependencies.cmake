# Shared implementation for setup and setup-deps.  It is invoked by CMake
# targets rather than at configure time, keeping configuration fast and safe.

if(NOT DEFINED PROJECT_ROOT OR NOT IS_DIRECTORY "${PROJECT_ROOT}")
    message(FATAL_ERROR "PROJECT_ROOT is required and must be a directory.")
endif()
if(NOT DEFINED PYTHON_EXECUTABLE OR NOT EXISTS "${PYTHON_EXECUTABLE}")
    message(FATAL_ERROR "PYTHON_EXECUTABLE is required.")
endif()
if(NOT DEFINED NPM_EXECUTABLE OR NOT EXISTS "${NPM_EXECUTABLE}")
    message(FATAL_ERROR "NPM_EXECUTABLE is required.")
endif()

set(_venv_python "${PROJECT_ROOT}/.venv/Scripts/python.exe")

function(run_checked description)
    execute_process(
        COMMAND ${ARGN}
        WORKING_DIRECTORY "${PROJECT_ROOT}"
        RESULT_VARIABLE _result
    )
    if(NOT _result EQUAL 0)
        message(FATAL_ERROR "${description} failed with exit code ${_result}.")
    endif()
endfunction()

function(run_checked_in working_directory description)
    execute_process(
        COMMAND ${ARGN}
        WORKING_DIRECTORY "${working_directory}"
        RESULT_VARIABLE _result
    )
    if(NOT _result EQUAL 0)
        message(FATAL_ERROR "${description} failed with exit code ${_result}.")
    endif()
endfunction()

if(NOT EXISTS "${_venv_python}")
    run_checked("Creating Python virtual environment"
        "${PYTHON_EXECUTABLE}" -m venv "${PROJECT_ROOT}/.venv")
endif()

execute_process(
    COMMAND "${_venv_python}" -c "import platform; print(platform.python_version())"
    OUTPUT_VARIABLE _venv_python_version
    OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE _venv_python_result
)
if(NOT _venv_python_result EQUAL 0 OR NOT _venv_python_version STREQUAL "3.12.4")
    message(FATAL_ERROR
        "The virtual environment must use Python 3.12.4; found '${_venv_python_version}'. Delete .venv and rerun setup.")
endif()

run_checked("Installing backend dependencies"
    "${_venv_python}" -m pip install --disable-pip-version-check -r backend/requirements.txt)
run_checked("Checking backend dependencies" "${_venv_python}" -m pip check)

run_checked_in("${PROJECT_ROOT}/frontend" "Installing frontend dependencies" "${NPM_EXECUTABLE}" ci)

message(STATUS "Python and frontend dependencies are installed.")


