include("${CMAKE_CURRENT_LIST_DIR}/SetupDependencies.cmake")

if(NOT DEFINED DATA_SOURCE_DIR OR NOT IS_DIRECTORY "${DATA_SOURCE_DIR}")
    message(FATAL_ERROR
        "CSpider source data was not found: '${DATA_SOURCE_DIR}'. Set CSPIDER_DATA_SOURCE_DIR and reconfigure.")
endif()
foreach(_required_file train.json train_gold.sql dev.json dev_gold.sql tables.json char_emb.txt README.txt)
    if(NOT EXISTS "${DATA_SOURCE_DIR}/${_required_file}")
        message(FATAL_ERROR "CSpider source data is incomplete. Missing: ${DATA_SOURCE_DIR}/${_required_file}")
    endif()
endforeach()
if(NOT IS_DIRECTORY "${DATA_SOURCE_DIR}/database")
    message(FATAL_ERROR "CSpider source data is incomplete. Missing: ${DATA_SOURCE_DIR}/database")
endif()

run_checked("Creating database-disjoint CSpider data splits"
    "${CMAKE_COMMAND}" -E env "CSPIDER_SOURCE_DIR=${DATA_SOURCE_DIR}"
    "${_venv_python}" split_cspider.py)

message(STATUS "Environment setup is complete. Start with: cmake --build build --target start")
