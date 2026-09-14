import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

from fdroidserver import metadata

from update_fdroid_config import CONFIG_TARGET, update_config


class UpdateConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.recipe = Path(self.directory.name) / 'com.example.app.yml'
        self.original_command = f"printf '%s' '{{}}' > {CONFIG_TARGET}"
        app = metadata.App({'License': 'GPL-3.0-or-later', 'Builds': [
            metadata.Build({'versionName': '0.6.0', 'versionCode': 29, 'init': [self.original_command]}),
            metadata.Build({'versionName': '0.6.5', 'versionCode': 33, 'init': [self.original_command]}),
        ]})
        metadata.write_metadata(self.recipe, app)
        self.config = json.dumps({
            'version': '0.6.5', 'bundle': {'android': {'versionCode': 33}},
            'description': "Profiles: Kemper; Captain's display; $HOME; `echo injected`; è",
        }, ensure_ascii=False, indent=2)

    def test_yaml_and_shell_round_trip_preserve_exact_config(self):
        update_config(self.recipe, self.config)
        app = metadata.parse_metadata(self.recipe)
        self.assertEqual(app.Builds[0].init, [self.original_command])
        command = app.Builds[1].init[0]
        self.assertEqual(shlex.split(command), ['printf', '%s', self.config, '>', CONFIG_TARGET])
        # Execute the printf through a real shell to catch expansion/quoting
        # errors that JSON or YAML parsing alone cannot expose.
        shell = shutil.which('bash')
        if shell:
            rendered = subprocess.check_output([shell], input=command.rsplit(' > ', 1)[0].encode('utf-8'))
            self.assertEqual(rendered, self.config.encode('utf-8'))
        first = self.recipe.read_bytes()
        update_config(self.recipe, self.config)
        self.assertEqual(self.recipe.read_bytes(), first)

    def test_wrong_release_does_not_change_recipe(self):
        before = self.recipe.read_bytes()
        config = json.loads(self.config)
        config['bundle']['android']['versionCode'] = 34
        with self.assertRaises(ValueError):
            update_config(self.recipe, json.dumps(config))
        self.assertEqual(self.recipe.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
