import unittest
from unittest.mock import Mock, mock_open, patch

from pwneye.cli.parser import parse_args
from pwneye.core import engine


class EmptyCredentialTests(unittest.TestCase):
    kb = {
        "ports": [554], "vendors": [], "common_paths": ["/live"],
        "common_creds": {"usernames": ["admin", "root"], "passwords": ["admin", "12345"]},
    }
    onvif_kb = {"ports": [80], "usernames": ["admin", "root"], "passwords": ["admin", "12345"]}

    def args(self, *options):
        with patch("sys.argv", ["pwneye", "-t", "192.0.2.1", *options]):
            return parse_args(Mock())

    def targets(self, *options, onvif_credentials=None):
        return engine._resolve_rtsp_targets(self.args(*options), self.kb, [554],
                                           onvif_credentials=onvif_credentials)

    def test_parser_distinguishes_omitted_from_empty(self):
        omitted = self.args()
        explicit = self.args("-u", "", "-p", "", "-ou", "", "-op", "")
        for name in ("username", "password", "onvif_username", "onvif_password"):
            self.assertIsNone(getattr(omitted, name))
            self.assertEqual(getattr(explicit, name), "")

    def test_literal_resolver(self):
        self.assertEqual(engine._resolve_credential_values(None), [])
        self.assertEqual(engine._resolve_credential_values(""), [""])
        self.assertEqual(engine._resolve_credential_values("admin"), ["admin"])

    def test_omitted_connection_string_still_uses_common_paths(self):
        self.assertEqual(engine._resolve_connection_string_values(""), [])
        self.assertEqual(self.targets()[3], ["/live"])

    def test_omitted_credentials_still_use_common_values(self):
        self.assertEqual(self.targets()[1:3], (["admin", "root"], ["admin", "12345"]))

    def test_empty_username_is_fixed(self):
        self.assertEqual(self.targets("-u", "")[1:3], ([""], ["admin", "12345"]))

    def test_empty_password_is_fixed(self):
        self.assertEqual(self.targets("-p", "")[1:3], (["admin", "root"], [""]))

    def test_admin_with_empty_password(self):
        self.assertEqual(self.targets("-u", "admin", "-p", "")[1:3], (["admin"], [""]))

    def test_both_empty_are_not_replaced_by_onvif(self):
        self.assertEqual(self.targets("-u", "", "-p", "", onvif_credentials=("onvif", "secret"))[1:3],
                         ([""], [""]))

    def test_empty_rtsp_credentials_bypass_cache(self):
        for options in (("-u", ""), ("-p", "")):
            with patch.object(engine.cachedata, "get_cached_rtsp_auth") as cached:
                self.assertFalse(engine._try_cached_rtsp_auth(
                    args=self.args(*options), cache_entry={}, onvif_credentials=None, tui=Mock()))
            cached.assert_not_called()

    def test_onvif_empty_values_are_fixed(self):
        for options, expected in (
            (("-ou", ""), ([""], ["admin", "12345"])),
            (("-op", ""), (["admin", "root"], [""])),
            (("-ou", "admin", "-op", ""), (["admin"], [""])),
            (("-ou", "", "-op", ""), ([""], [""])),
        ):
            self.assertEqual(engine._resolve_onvif_targets(self.args(*options), self.onvif_kb)[1:], expected)

    def test_onvif_empty_uses_explicit_phase_not_cache(self):
        tui = Mock()
        tui.confirm.return_value = False
        with patch.object(engine.cachedata, "get_cached_onvif_auth") as cached, \
             patch.object(engine, "_detect_onvif_camera", return_value=(None, None, None, [80])) as detect:
            engine._run_onvif_scan(self.args("-ou", "", "-op", ""), self.onvif_kb, {}, tui)
        cached.assert_not_called()
        detect.assert_called_once()
        self.assertIn("user-provided", detect.call_args.kwargs["auth_label"])
        self.assertIsNone(detect.call_args.kwargs["auth_hint"])
        tui.confirm.assert_called_once()

    def test_wordlists_still_ignore_blank_lines(self):
        with patch.object(engine.Path, "is_file", return_value=True), \
             patch.object(engine.Path, "open", mock_open(read_data="admin\n\nroot\r\n")):
            self.assertEqual(engine._resolve_credential_values("users.txt"), ["admin", "root"])

    def test_empty_wordlist_does_not_expand_to_common_credentials(self):
        with patch.object(engine.Path, "is_file", return_value=True), \
             patch.object(engine.Path, "open", mock_open(read_data="")):
            self.assertEqual(self.targets("-u", "users.txt")[1], [])


if __name__ == "__main__":
    unittest.main()
