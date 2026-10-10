#ifndef BOSUN_PLUGIN_KINDS_H
#define BOSUN_PLUGIN_KINDS_H

/* Profile kinds the native runtime accepts. The list lives in the source tree,
 * not the build tree, so tools that compile config.c without CMake (the
 * Raspberry Pi storage converter) need no generated files.
 * cmake/build_manifest.py fails the build when it differs from the plugins in
 * the native manifest. */
static const char *const bosun_plugin_kinds[] = {"generic_midi", "kemper_player", "kemper_head"};

#endif
