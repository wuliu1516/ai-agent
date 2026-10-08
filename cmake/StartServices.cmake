if(NOT DEFINED PROJECT_ROOT OR NOT IS_DIRECTORY "${PROJECT_ROOT}")
    message(FATAL_ERROR "PROJECT_ROOT is required.")
endif()
foreach(_name PYTHON_EXECUTABLE NODE_EXECUTABLE)
    if(NOT DEFINED ${_name} OR NOT EXISTS "${${_name}}")
        message(FATAL_ERROR "${_name} is required.")
    endif()
endforeach()

execute_process(
    COMMAND powershell.exe -NoProfile -ExecutionPolicy Bypass -File
        "${PROJECT_ROOT}/cmake/StartServices.ps1"
        -ProjectRoot "${PROJECT_ROOT}"
        -PythonExecutable "${PYTHON_EXECUTABLE}"
        -NodeExecutable "${NODE_EXECUTABLE}"
    RESULT_VARIABLE _result
)
if(NOT _result EQUAL 0)
    message(FATAL_ERROR "Starting services failed with exit code ${_result}.")
endif()
