add_library(bosun_board STATIC board.c cdc_session.c usb_rx_trace.c usb_descriptors.c)
target_include_directories(bosun_board PUBLIC
    "${CMAKE_CURRENT_LIST_DIR}/../../include" "${CMAKE_CURRENT_LIST_DIR}")
target_compile_definitions(bosun_board PUBLIC BOSUN_PLATFORM_RP2040=1)
target_compile_options(bosun_board PRIVATE -Wall -Wextra -Werror)
target_link_libraries(bosun_board PUBLIC
    pico_stdlib pico_unique_id hardware_adc hardware_clocks hardware_dma
    hardware_flash hardware_irq hardware_pio hardware_pwm hardware_spi
    hardware_sync hardware_uart hardware_watchdog tinyusb_device)
pico_generate_pio_header(bosun_board "${CMAKE_CURRENT_LIST_DIR}/ws2812.pio")

function(bosun_configure_rp2040_target target)
    target_link_libraries(${target} PRIVATE bosun_board)
    pico_enable_stdio_usb(${target} 0)
    pico_enable_stdio_uart(${target} 0)
    # Keep the physical size macro intact. Only the linker FLASH region is
    # shortened, so board_storage_offset() still addresses the real tail.
    if(NOT PICO_FLASH_SIZE_BYTES MATCHES "^[0-9]+$" OR
       PICO_FLASH_SIZE_BYTES LESS 786432 OR PICO_FLASH_SIZE_BYTES GREATER 16777216)
        message(FATAL_ERROR "PICO_FLASH_SIZE_BYTES must be integer bytes in [786432,16777216]")
    endif()
    math(EXPR BOSUN_PROGRAM_FLASH_BYTES "${PICO_FLASH_SIZE_BYTES} - 524288")
    math(EXPR remainder "${PICO_FLASH_SIZE_BYTES} % 4096")
    if(NOT remainder EQUAL 0)
        message(FATAL_ERROR "PICO_FLASH_SIZE_BYTES must be sector aligned")
    endif()
    set(link_dir "${CMAKE_CURRENT_BINARY_DIR}/${target}-link")
    file(MAKE_DIRECTORY "${link_dir}")
    configure_file("${PROJECT_SOURCE_DIR}/cmake/pico_flash_region.ld.in"
                   "${link_dir}/pico_flash_region.ld" @ONLY)
    pico_add_linker_script_override_path(${target} "${link_dir}" FILES pico_flash_region.ld)
    pico_add_linker_script_override_path(${target} "${PROJECT_SOURCE_DIR}/cmake" FILES section_extra_post_end.incl)
    # RP2040 has 264 KiB of contiguous SRAM. The SDK's default scratch Y is
    # only 4 KiB: explicitly partition 248 KiB data + 16 KiB core-0 stack.
    # Core 1 and scratch X are unavailable in this single-core application.
    target_compile_definitions(${target} PRIVATE PICO_STACK_SIZE=16384 PICO_HEAP_SIZE=0)
    target_link_options(${target} PRIVATE "LINKER:--print-memory-usage"
        "LINKER:--wrap,dcd_edpt_xfer"
        "LINKER:--wrap,dcd_event_handler"
        "LINKER:--wrap,cdcd_xfer_cb"
        "LINKER:--defsym,RAM_LENGTH=253952"
        "LINKER:--defsym,SCRATCH_X_ORIGIN=0x2003e000"
        "LINKER:--defsym,SCRATCH_X_LENGTH=0"
        "LINKER:--defsym,SCRATCH_Y_ORIGIN=0x2003e000"
        "LINKER:--defsym,SCRATCH_Y_LENGTH=16384"
        "LINKER:--defsym,HEAP_LIMIT=__end__")
    pico_set_program_name(${target} "Bosun Native MIDI Captain")
    pico_set_program_version(${target} "0.1.0-experimental")
    pico_add_extra_outputs(${target})
    if(NOT PICO_NO_PICOTOOL AND NOT PICO_NO_UF2)
        find_package(Python3 REQUIRED COMPONENTS Interpreter)
        add_custom_command(TARGET ${target} POST_BUILD
            COMMAND "${Python3_EXECUTABLE}" "${PROJECT_SOURCE_DIR}/cmake/check_image.py"
                --elf "$<TARGET_FILE:${target}>"
                --uf2 "$<TARGET_FILE_DIR:${target}>/${target}.uf2"
                --flash-bytes "${PICO_FLASH_SIZE_BYTES}" --nm "${CMAKE_NM}"
                --report "$<TARGET_FILE_DIR:${target}>/${target}-memory.json"
            VERBATIM)
    endif()
endfunction()
