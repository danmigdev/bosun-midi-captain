# Frozen CircuitPython resources

CircuitPython firmware is retired. Use the maintained
[native firmware](../firmware-native/README.md) for new firmware features.

This directory retains the shared message/plugin schemas consumed by the native
build, the reference implementation used by differential tests, and the resources
still bundled for legacy installation and recovery paths. Keep the complete
repository when building Bosun; do not copy these files over a native installation.

New factory installations use the native Desktop installer directly. Follow the
[first-installation guide](../docs/first-setup.md); no CircuitPython bootstrap is
needed.

To migrate a Captain already running CircuitPython, follow the
[firmware update guide](../docs/firmware-updates.md).
