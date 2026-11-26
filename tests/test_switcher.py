import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import switcher as s

class TestSwitcher(unittest.TestCase):
    def test_relative_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/"config.js").touch()
            self.assertEqual(s.checked_path(root,"config.js"),root/"config.js")
            for path in ("../config.js","/etc/passwd","config.js\nINJECT=x"):
                with self.assertRaises(s.SwitchError):
                    s.checked_path(root,path)

    def test_trusted_symlink_target(self):
        with tempfile.TemporaryDirectory() as d:
            base=Path(d)
            root=base/"MagicMirror"
            trusted=base/"magicmirror-config"
            root.mkdir()
            trusted.mkdir()
            (trusted/"config.js").touch()
            (root/"config.js").symlink_to(trusted/"config.js")
            with self.assertRaises(s.SwitchError):
                s.checked_path(root,"config.js")
            self.assertEqual(s.checked_path(root,"config.js",[trusted]),trusted/"config.js")
            (base/"other.js").touch()
            (root/"other.js").symlink_to(base/"other.js")
            with self.assertRaises(s.SwitchError):
                s.checked_path(root,"other.js",[trusted])

    def test_environment(self):
        self.assertEqual(s.profile_env("dojo",{"config":"config/config.js","css":None}),
                         "MM_SWITCHER_PROFILE=dojo\nMM_CONFIG_FILE=config/config.js\n")
        self.assertIn("MM_CUSTOMCSS_FILE=config/sm.css",s.profile_env("sm",{"config":"config/sm.js","css":"config/sm.css"}))

    def test_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            target=Path(d)/"active.env"
            s.atomic(target,"one")
            s.atomic(target,"two")
            self.assertEqual(target.read_text(),"two")
            self.assertEqual(target.stat().st_mode & 0o777,0o600)

    def test_switch_and_rollback(self):
        profiles={"dojo":{"config":"config/config.js"},"sm":{"config":"config/sm.js"}}
        cfg={"profiles":profiles}
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with patch.multiple(s,STATE=root,ENV=root/"active.env",PENDING=root/"pending.json"), \
                 patch.object(s,"settings",return_value=cfg), \
                 patch.object(s,"assert_installed"), \
                 patch.object(s,"validate",side_effect=lambda c,n:profiles[n]), \
                 patch.object(s,"restart",side_effect=[s.SwitchError("test failure"),None]):
                s.atomic(s.ENV,s.profile_env("dojo",profiles["dojo"]))
                with self.assertRaisesRegex(s.SwitchError,"previous profile restored"):
                    s.activate("sm")
                self.assertEqual(s.selected(),"dojo")
                self.assertFalse(s.PENDING.exists())

    def test_successful_switch(self):
        profiles={"dojo":{"config":"config/config.js"},"sm":{"config":"config/sm.js"}}
        cfg={"profiles":profiles}
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with patch.multiple(s,STATE=root,ENV=root/"active.env",PENDING=root/"pending.json"), \
                 patch.object(s,"settings",return_value=cfg), \
                 patch.object(s,"assert_installed"), \
                 patch.object(s,"validate",side_effect=lambda c,n:profiles[n]), \
                 patch.object(s,"restart"):
                s.atomic(s.ENV,s.profile_env("dojo",profiles["dojo"]))
                self.assertEqual(s.activate("sm"),"sm")
                self.assertEqual(s.selected(),"sm")
                self.assertFalse(s.PENDING.exists())

if __name__=="__main__":
    unittest.main()
