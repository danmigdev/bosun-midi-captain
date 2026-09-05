# The native experiment uses a reproducible external SDK. Never download a
# moving branch or alter a user's SDK checkout as a side effect of configure.
if(NOT PICO_SDK_PATH AND DEFINED ENV{PICO_SDK_PATH})
    set(PICO_SDK_PATH "$ENV{PICO_SDK_PATH}" CACHE PATH "Pico SDK 2.3.0 checkout")
endif()
if(NOT EXISTS "${PICO_SDK_PATH}/pico_sdk_init.cmake")
    message(FATAL_ERROR "Set PICO_SDK_PATH to the pinned Pico SDK 2.3.0 checkout; see tools/native-build.sh")
endif()
find_package(Git REQUIRED)
execute_process(COMMAND "${GIT_EXECUTABLE}" -C "${PICO_SDK_PATH}" rev-parse HEAD
    OUTPUT_VARIABLE BOSUN_SDK_COMMIT OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)
if(NOT BOSUN_SDK_COMMIT STREQUAL "98a542c1a62fb549ffb5d66a3e5892b06276b670")
    message(FATAL_ERROR "Pico SDK must be the exact 2.3.0 commit, found ${BOSUN_SDK_COMMIT}")
endif()
execute_process(COMMAND "${GIT_EXECUTABLE}" -C "${PICO_SDK_PATH}/lib/tinyusb" rev-parse HEAD
    OUTPUT_VARIABLE BOSUN_TINYUSB_COMMIT OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)
if(NOT BOSUN_TINYUSB_COMMIT STREQUAL "86ad6e56c1700e85f1c5678607a762cfe3aa2f47")
    message(FATAL_ERROR "Initialize Pico SDK's pinned lib/tinyusb submodule")
endif()
set(PICO_BOARD bosun_midi_captain CACHE STRING "Verified MIDI Captain pin map")
list(APPEND PICO_BOARD_HEADER_DIRS "${CMAKE_CURRENT_LIST_DIR}/boards")
set(PICO_PLATFORM rp2040 CACHE STRING "MIDI Captain MCU")
set(PICO_FLASH_SIZE_BYTES 2097152 CACHE STRING "Physical flash bytes; verify hardware before installing")
# SDK 2.3.0 uses picotool to generate UF2. Pin the tool as well as the SDK;
# this is its official 2.3.0 release commit, never a moving branch.
set(PICOTOOL_GIT_BRANCH 6f6458d792b93685a11423b244a585eaa99eafcf CACHE STRING "Pinned picotool 2.3.0")
include("${PICO_SDK_PATH}/pico_sdk_init.cmake")
