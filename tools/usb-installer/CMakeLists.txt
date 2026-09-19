cmake_minimum_required(VERSION 3.20)
include(../../firmware-native/cmake/pico_sdk.cmake)
project(BosunUsbInstaller LANGUAGES C CXX ASM)
pico_sdk_init()
add_executable(bosun_usb_installer main.c protocol.c usb_descriptors.c)
target_compile_options(bosun_usb_installer PRIVATE -Wall -Wextra -Werror)
target_link_libraries(bosun_usb_installer PRIVATE pico_stdlib pico_unique_id hardware_flash hardware_sync hardware_watchdog)
# BOOTSEL loads every UF2 block into SRAM. Starting this helper never erases flash.
pico_set_binary_type(bosun_usb_installer no_flash)
pico_enable_stdio_usb(bosun_usb_installer 1)
pico_enable_stdio_uart(bosun_usb_installer 0)
target_compile_definitions(bosun_usb_installer PRIVATE
    USBD_VID=0x239a USBD_PID=0x80f4
    USBD_PRODUCT="Bosun USB Installer" USBD_MANUFACTURER="Bosun"
    PICO_STDIO_USB_USE_DEFAULT_DESCRIPTORS=0
    PICO_ENABLE_USB_RESET_VIA_VENDOR_INTERFACE=0
    PICO_ENABLE_USB_RESET_VIA_BAUD_RATE=0
    PICO_STDIO_USB_DEFAULT_CRLF=0)
pico_add_extra_outputs(bosun_usb_installer)
