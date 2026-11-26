"""The completion scripts and the man page under share/ name every command and option of the binary."""

import os
import re
import unittest

from tests.support import run_cli

SHARE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "share")


def read(*parts):
    with open(os.path.join(SHARE, *parts), encoding="utf-8") as handle:
        return handle.read()


class ShareTests(unittest.TestCase):
    def commands(self):
        top = run_cli("--help")
        found = re.search(r"\{([a-z,]+)\}", top.stdout)
        self.assertIsNotNone(found, top.stdout)
        return found.group(1).split(",")

    def test_every_command_and_option_is_in_each_file(self):
        bash = read("completions", "eb-staged-rebuild.bash")
        zsh = read("completions", "_eb-staged-rebuild")
        man = read("man", "eb-staged-rebuild.1").replace("\\-", "-")
        commands = self.commands()
        self.assertGreaterEqual(len(commands), 10)
        for command in commands:
            for name, text in (("bash", bash), ("zsh", zsh), ("man", man)):
                self.assertIn(command, text, f"{command} is missing from the {name} file")
            helped = run_cli(command, "--help")
            self.assertEqual(helped.returncode, 0, helped.stderr)
            for option in sorted(set(re.findall(r"--[a-z][a-z0-9-]+", helped.stdout))):
                if option == "--help":
                    continue
                for name, text in (("bash", bash), ("zsh", zsh), ("man", man)):
                    self.assertIn(option, text, f"{command} {option} is missing from the {name} file")


if __name__ == "__main__":
    unittest.main()
