"""The EasyBuild hook a site points EASYBUILD_HOOKS at."""

from __future__ import annotations

import os
import unittest

from eb_staged_rebuild.site_hooks import (
    enable_rpath,
    footer_text,
    outside_prefix,
    pre_build_hook,
    pre_install_hook,
    pre_module_hook,
)


class SiteHookTests(unittest.TestCase):
    def test_an_install_outside_the_prefix_is_named(self):
        self.assertFalse(outside_prefix("/opt/site", "/opt/site/software/Foo/1.0"))
        self.assertFalse(outside_prefix("/opt/site", "/opt/site"))
        self.assertTrue(outside_prefix("/opt/site", "/opt/site-extra/Foo/1.0"))
        self.assertTrue(outside_prefix("/opt/site", "/tmp/Foo/1.0"))
        self.assertFalse(outside_prefix("", "/tmp/Foo/1.0"))

    def test_the_prefix_check_is_off_until_a_prefix_is_set(self):
        env = os.environ.copy()
        for name in ("EB_LOCAL_PREFIX", "EESSI_SITE_SOFTWARE_PATH"):
            env.pop(name, None)
        self.assertIsNone(pre_install_hook(type("B", (), {"installdir": "/tmp/Foo"})(), environ=env))
        env["EB_LOCAL_PREFIX"] = "/opt/site"
        with self.assertRaises(RuntimeError) as caught:
            pre_install_hook(type("B", (), {"installdir": "/tmp/Foo/1.0"})(), environ=env)
        self.assertIn("/tmp/Foo/1.0", str(caught.exception))
        self.assertIn("/opt/site", str(caught.exception))
        self.assertIsNone(pre_install_hook(type("B", (), {"installdir": "/opt/site/software/Foo/1.0"})(), environ=env))
        env.pop("EB_LOCAL_PREFIX")
        env["EESSI_SITE_SOFTWARE_PATH"] = "/opt/site"
        with self.assertRaises(RuntimeError):
            pre_install_hook(type("B", (), {"installdir": "/tmp/Foo"})(), environ=env)

    def test_the_footer_and_the_rpath_wait_for_their_variables(self):
        self.assertEqual(footer_text("already", "site line"), "already\nsite line\n")
        self.assertEqual(footer_text("already\nsite line\n", "site line"), "already\nsite line\n")
        self.assertEqual(footer_text("", ""), "")
        block = type("B", (), {"cfg": {"module_footer": ""}})()
        pre_module_hook(block, environ={})
        self.assertEqual(block.cfg["module_footer"], "")
        pre_module_hook(block, environ={"EB_LOCAL_MODULE_FOOTER": "site line"})
        self.assertEqual(block.cfg["module_footer"], "site line\n")
        options = {}
        self.assertIsNone(enable_rpath(options, False))
        self.assertEqual(options, {})
        enable_rpath(options, True)
        self.assertTrue(options["rpath"])
        build = type("B", (), {"toolchain": type("T", (), {"options": {}})()})()
        pre_build_hook(build, environ={})
        self.assertEqual(build.toolchain.options, {})
        pre_build_hook(build, environ={"EB_LOCAL_RPATH": "1"})
        self.assertTrue(build.toolchain.options["rpath"])

    def test_the_how_to_names_the_hook_file(self):
        page = (Path := __import__("pathlib").Path)(__file__).resolve().parents[1] / "docs" / "orgmode" / "howto" / "add-a-site-hook.org"
        text = page.read_text()
        self.assertIn("EASYBUILD_HOOKS", text)
        self.assertIn("EB_LOCAL_PREFIX", text)
        self.assertIn("EB_LOCAL_MODULE_FOOTER", text)


if __name__ == "__main__":
    unittest.main()
