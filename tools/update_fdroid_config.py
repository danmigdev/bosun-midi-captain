#!/usr/bin/env python3
"""Copy a release APK's exact Tauri configuration into its F-Droid build."""

import argparse
import json
import os
from pathlib import Path
import shlex
import tempfile

from fdroidserver import metadata


CONFIG_TARGET = 'src-tauri/gen/android/app/src/main/assets/tauri.conf.json'


def update_config(recipe: Path, config_text: str) -> None:
    config = json.loads(config_text)
    version = config['version']
    code = config['bundle']['android']['versionCode']
    app = metadata.parse_metadata(recipe)
    matches = []
    for build in app.Builds:
        if build.versionName != version or build.versionCode != code:
            continue
        for index, command in enumerate(build.init):
            words = shlex.split(command)
            if len(words) == 5 and words[:2] == ['printf', '%s'] and words[3:] == ['>', CONFIG_TARGET]:
                matches.append((build, index))
    if len(matches) != 1:
        raise ValueError(f'Expected one Tauri config command for {version} ({code}); found {len(matches)}')
    build, index = matches[0]
    build.init[index] = f"printf '%s' {shlex.quote(config_text)} > {CONFIG_TARGET}"
    # Let the YAML serializer escape colons, quotes and newlines. Replacing
    # text inside YAML directly can turn JSON descriptions into YAML mappings.
    with tempfile.TemporaryDirectory(dir=recipe.parent) as directory:
        updated = Path(directory) / recipe.name
        with updated.open('w', encoding='utf-8', newline='\n') as stream:
            metadata.write_yaml(stream, app)
        os.replace(updated, recipe)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('recipe', type=Path)
    parser.add_argument('config', type=Path)
    args = parser.parse_args()
    update_config(args.recipe, args.config.read_text(encoding='utf-8'))
