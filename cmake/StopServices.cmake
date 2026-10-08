if(NOT DEFINED PROJECT_ROOT OR NOT IS_DIRECTORY "${PROJECT_ROOT}")
    message(FATAL_ERROR "PROJECT_ROOT is required.")
endif()
execute_process(
    COMMAND powershell.exe -NoProfile -ExecutionPolicy Bypass -File
        "${PROJECT_ROOT}/cmake/StopServices.ps1"
        -ProjectRoot "${PROJECT_ROOT}"
    RESULT_VARIABLE _result
)
if(NOT _result EQUAL 0)
    message(FATAL_ERROR "Stopping services failed with exit code ${_result}.")
endif()

