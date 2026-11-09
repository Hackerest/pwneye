import argparse
import io
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rich.console import Console
from rich.text import Text

from pwneye.cli.parser import parse_args
from pwneye.cli.tui import TUI
from pwneye.core import engine
from pwneye.core.network import interfaces, onvif, rtsp
from pwneye.core.types import ExitCode, RtspAttempt, RtspProbeResult


def probe_result(available=False, status=404, error=None):
    return RtspProbeResult("rtsp://192.0.2.1/test", status, "test", None,
                           status not in (401, 403), available, available, error)


def attempt():
    return RtspAttempt("192.0.2.1", 554, "/stream?channel=1", "admin", "test", "tcp",
                       "rtsp://admin:test@192.0.2.1:554/stream?channel=1")


class OptionsTests(unittest.TestCase):
    def parse(self, *options):
        with patch("sys.argv", ["pwneye", "-t", "192.0.2.1", *options]):
            return parse_args(Mock())

    def test_skip_alias_does_not_conflict_with_protocol_flags(self):
        for option in ("-srt", "--skip-reachability"):
            args = self.parse(option)
            self.assertTrue(args.skip_reachability)
            self.assertFalse(args.skip_rtsp)
            self.assertFalse(args.skip_onvif)
        args = self.parse("-sr", "-so")
        self.assertFalse(args.skip_reachability)
        self.assertTrue(args.skip_rtsp)
        self.assertTrue(args.skip_onvif)

    def test_invalid_timeout(self):
        for value in ("0", "-1"):
            with self.assertRaises(SystemExit), patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                self.parse("--timeout", value)

    def test_separate_default_timeouts(self):
        args = self.parse()
        self.assertEqual(args.rtsp_port_timeout, 5)
        self.assertEqual(args.timeout, 10)

    def test_explicit_timeout_overrides_both_defaults(self):
        for value in (2, 5, 10, 20):
            args = self.parse("--timeout", str(value))
            self.assertEqual(args.rtsp_port_timeout, value)
            self.assertEqual(args.timeout, value)

    def test_reachability_skipped_without_ping_or_prompt(self):
        tui = Mock()
        with patch.object(engine.netcomm, "is_host_reachable") as ping:
            self.assertTrue(engine._check_target_reachability(self.parse("-srt"), tui))
            ping.assert_not_called()
        tui.confirm.assert_not_called()
        tui.warning.assert_called_once_with("Skipping the initial reachability check as requested")

    def test_reachability_default_unchanged(self):
        tui = Mock()
        tui.confirm.return_value = False
        with patch.object(engine.netcomm, "is_host_reachable", return_value=False) as ping:
            self.assertFalse(engine._check_target_reachability(self.parse(), tui))
            ping.assert_called_once_with("192.0.2.1")
        tui.confirm.assert_called_once()


class ProgressTests(unittest.TestCase):
    def test_bold_space_padded_counter_and_escaped(self):
        tui = TUI(Console(file=io.StringIO()))
        with patch.object(tui, "update_live") as update:
            tui.update_attempt_progress(7, 100, "Trying [red]camera[/red]")
        rendered = Text.from_markup(update.call_args.args[0])
        self.assertEqual(rendered.plain, "[  7%] [  7/100] Trying [red]camera[/red]")
        self.assertTrue(any(span.style == "bold" and span.start == 0 for span in rendered.spans))

    def test_progress_width_is_fixed_within_each_phase(self):
        tui = TUI(Console(file=io.StringIO()))
        for total in (0, 1, 9, 10, 100, 7560):
            with patch.object(tui, "update_live") as update:
                for completed in sorted({0, total // 100, total // 10, total // 2, total}):
                    tui.update_attempt_progress(completed, total, "Trying camera")
            lines = [Text.from_markup(call.args[0]).plain for call in update.call_args_list]
            self.assertEqual(len({line.index("Trying camera") for line in lines}), 1)
            width = len(str(total))
            self.assertIn(f"[{0:>{width}}/{total}]", lines[0])
            self.assertIn(f"[{total}/{total}]", lines[-1])
            self.assertTrue(lines[-1].startswith("[100%]" if total else "[  0%]"))

    def test_rtsp_counts_completions_with_threads(self):
        tui = Mock()
        with patch.object(rtsp, "probe_rtsp_url", return_value=probe_result()):
            engine._run_rtsp_bruteforce([attempt()] * 20, 1, 4, False, tui)
        values = [call.args[0] for call in tui.update_attempt_progress.call_args_list]
        self.assertEqual(values, sorted(values))
        self.assertEqual((values[0], values[-1]), (0, 20))
        self.assertTrue(all(call.args[1] == 20 for call in tui.update_attempt_progress.call_args_list))

    def test_rtsp_early_success_does_not_force_completion(self):
        tui = Mock()
        with patch.object(rtsp, "probe_rtsp_url", return_value=probe_result(True, 200)):
            engine._run_rtsp_bruteforce([attempt()] * 10, 1, 1, False, tui)
        self.assertEqual(tui.update_attempt_progress.call_args.args[:2], (1, 10))

    def test_rtsp_exception_counts_once(self):
        tui = Mock()
        with patch.object(rtsp, "probe_rtsp_url", side_effect=OSError("test")):
            engine._run_rtsp_bruteforce([attempt()] * 3, 1, 1, False, tui)
        self.assertEqual(tui.update_attempt_progress.call_args.args[:2], (3, 3))

    def test_onvif_progress_uses_responsive_ports(self):
        progress = Mock()
        with patch.object(onvif, "try_onvif_connection", return_value=None):
            onvif.detect("192.0.2.1", [80, 8000], ["a", "b"], ["x", "y"],
                         threads=3, responsive_ports=[80], on_progress=progress)
        values = [call.args[0] for call in progress.call_args_list]
        self.assertEqual(values, sorted(values))
        self.assertEqual((values[0], values[-1]), (0, 4))
        self.assertTrue(all(call.args[1] == 4 for call in progress.call_args_list))

    def test_onvif_early_success(self):
        progress = Mock()
        with patch.object(onvif, "try_onvif_connection", return_value=object()):
            onvif.detect("192.0.2.1", [80], ["a", "b"], ["x", "y"],
                         responsive_ports=[80], on_progress=progress)
        self.assertEqual(progress.call_args.args[:2], (1, 4))


class PortTimeoutTests(unittest.TestCase):
    def test_shared_deadline_and_fragmented_status(self):
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock(return_value=False)
        sock.recv.side_effect = [b"RT", b"SP/1.0"]
        with patch.object(rtsp.socket, "socket", return_value=sock), \
             patch.object(rtsp.time, "monotonic", side_effect=[0, 0, 2, 3, 4]):
            self.assertTrue(rtsp.is_rtsp_port("192.0.2.1", 554, timeout=10))
        self.assertEqual([c.args[0] for c in sock.settimeout.call_args_list], [10, 8, 7, 6])
        sock.__exit__.assert_called_once()

    def test_deadline_does_not_restart_after_connect(self):
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock(return_value=False)
        with patch.object(rtsp.socket, "socket", return_value=sock), \
             patch.object(rtsp.time, "monotonic", side_effect=[0, 0, 11]):
            self.assertFalse(rtsp.is_rtsp_port("192.0.2.1", 554, timeout=10))
        sock.sendall.assert_not_called()
        sock.__exit__.assert_called_once()

    def test_socket_closed_on_timeout(self):
        sock = Mock()
        sock.__enter__ = Mock(return_value=sock)
        sock.__exit__ = Mock(return_value=False)
        sock.connect.side_effect = socket.timeout()
        with patch.object(rtsp.socket, "socket", return_value=sock):
            self.assertFalse(rtsp.is_rtsp_port("192.0.2.1", 554, timeout=1))
        sock.__exit__.assert_called_once()

    def test_timeout_forwarded_for_all_port_sources(self):
        for options in ({"preferred_port": 8554},
                        {"onvif_streams": ["rtsp://192.0.2.1:8554/test"]}, {}):
            with patch.object(rtsp, "is_rtsp_port", return_value=True) as probe:
                engine._resolve_rtsp_ports("192.0.2.1", {"ports": [554]}, Mock(), timeout=7, **options)
            self.assertTrue(probe.called)
            self.assertTrue(all(c.kwargs["timeout"] == 7 for c in probe.call_args_list))


class DiscoveryTests(unittest.TestCase):
    selections = [interfaces.InterfaceSelection("en0", "192.0.2.2", 24),
                  interfaces.InterfaceSelection("utun0", "198.51.100.2", 24)]

    def test_interface_filtering(self):
        adapters = [SimpleNamespace(name="lo0", ips=[SimpleNamespace(ip="127.0.0.1", network_prefix=8)]),
                    SimpleNamespace(name="en0", ips=[SimpleNamespace(ip=("::1", 0, 0), network_prefix=128),
                                                     SimpleNamespace(ip="192.0.2.2", network_prefix=24)]),
                    SimpleNamespace(name="empty", ips=[])]
        with patch.object(interfaces.ifaddr, "get_adapters", return_value=adapters):
            self.assertEqual(interfaces.list_discovery_interfaces(), self.selections[:1])

    def test_cycles_and_keeps_same_ip_on_different_interfaces(self):
        tui = Mock()
        args = argparse.Namespace(discover="all", no_cache=True)
        device = {"host": "192.0.2.10", "port": 80, "xaddrs": []}
        with patch.object(interfaces, "list_discovery_interfaces", return_value=self.selections), \
             patch.object(onvif, "discover", side_effect=[[device], [device], [device], KeyboardInterrupt]) as discover, \
             patch.object(engine.time, "sleep"), \
             patch.object(engine, "_print_onvif_discovery_device") as render:
            result = engine._run_onvif_discovery(args, tui)
        self.assertEqual(result, ExitCode.SUCCESS)
        self.assertEqual([c.kwargs["interface"] for c in discover.call_args_list],
                         ["192.0.2.2", "198.51.100.2", "192.0.2.2", "198.51.100.2"])
        self.assertEqual(render.call_count, 2)
        tui.block.assert_called_once_with({"en0": "192.0.2.0/24", "utun0": "198.51.100.0/24"})
        self.assertIn("[1/2] (pass 2)", tui.update_live.call_args_list[2].args[0])

    def test_interface_list_without_subnet(self):
        tui = Mock()
        with patch.object(interfaces, "list_discovery_interfaces", return_value=[
            interfaces.InterfaceSelection("en0", "192.0.2.2", None)
        ]), patch.object(onvif, "discover", side_effect=KeyboardInterrupt):
            engine._run_onvif_discovery(argparse.Namespace(discover="all"), tui)
        tui.block.assert_called_once_with({"en0": "Subnet unavailable"})

    def test_no_interfaces(self):
        with patch.object(interfaces, "list_discovery_interfaces", return_value=[]), \
             patch.object(onvif, "discover") as discover:
            self.assertEqual(engine._run_onvif_discovery(argparse.Namespace(discover="all"), Mock()), ExitCode.FAILURE)
        discover.assert_not_called()

    def test_default_without_resolvable_interface(self):
        with patch.object(interfaces, "get_default_interface", return_value=interfaces.InterfaceSelection(None, None, None)), \
             patch.object(onvif, "discover", side_effect=KeyboardInterrupt) as discover:
            engine._run_onvif_discovery(argparse.Namespace(discover=""), Mock())
        discover.assert_called_once_with(interface=None)

    def test_error_does_not_stop_next_interface(self):
        with patch.object(interfaces, "list_discovery_interfaces", return_value=self.selections), \
             patch.object(onvif, "discover", side_effect=[OSError("failed"), KeyboardInterrupt]) as discover:
            engine._run_onvif_discovery(argparse.Namespace(discover="all"), Mock())
        self.assertEqual(discover.call_count, 2)

    def test_existing_modes_preserved(self):
        for value in ("", "en0"):
            with patch.object(interfaces, "get_default_interface", return_value=self.selections[0]), \
                 patch.object(interfaces, "resolve_interface_selection", return_value=self.selections[0]), \
                 patch.object(onvif, "discover", side_effect=KeyboardInterrupt) as discover:
                engine._run_onvif_discovery(argparse.Namespace(discover=value), Mock())
            discover.assert_called_once_with(interface="192.0.2.2")


class ChannelTests(unittest.TestCase):
    def check(self, result, tui):
        with patch.object(engine, "_probe_rtsp_attempt", return_value=result):
            return engine._check_rtsp_channel_enumeration(attempt(), "/stream?channel={channel}", 1,
                                                         argparse.Namespace(timeout=2), tui)

    def test_catch_all_returns_only_original_and_sentinels_are_silent(self):
        tui = Mock()
        tui.confirm.return_value = False
        with patch.object(engine, "_probe_rtsp_attempt", return_value=probe_result(True, 200)) as probe:
            channels, interrupted = engine._discover_rtsp_channels(attempt(), argparse.Namespace(timeout=2), tui)
        self.assertEqual([c.channel for c in channels], [1])
        self.assertFalse(interrupted)
        tui.confirm.assert_called_once_with("Continue channel enumeration anyway?", default=False)
        self.assertEqual(probe.call_count, 2)
        tui.success.assert_not_called()
        self.assertNotIn("1337", str(tui.mock_calls))
        self.assertNotIn("31337", str(tui.mock_calls))

    def test_invalid_paths_suggest_support(self):
        tui = Mock()
        self.assertTrue(self.check(probe_result(), tui))
        tui.info2.assert_called_once()
        tui.warning.assert_not_called()

    def test_catch_all_can_be_overridden(self):
        tui = Mock()
        tui.confirm.return_value = True
        self.assertTrue(self.check(probe_result(True, 200), tui))
        tui.confirm.assert_called_once_with("Continue channel enumeration anyway?", default=False)

    def test_interrupt_at_override_retains_original(self):
        tui = Mock()
        tui.confirm.side_effect = KeyboardInterrupt
        with patch.object(engine, "_probe_rtsp_attempt", return_value=probe_result(True, 200)):
            channels, interrupted = engine._discover_rtsp_channels(attempt(), argparse.Namespace(timeout=2), tui)
        self.assertTrue(interrupted)
        self.assertEqual([c.channel for c in channels], [1])

    def test_one_high_channel_is_not_enough_to_block_enumeration(self):
        tui = Mock()
        with patch.object(engine, "_probe_rtsp_attempt", side_effect=[probe_result(True, 200), probe_result(), probe_result()]):
            self.assertTrue(engine._check_rtsp_channel_enumeration(
                attempt(), "/stream?channel={channel}", 1, argparse.Namespace(timeout=2), tui))
        tui.info2.assert_not_called()
        tui.warning.assert_called_once()

    def test_timeout_or_server_error_is_not_proof(self):
        for result in (probe_result(status=None, error="timeout"), probe_result(status=500)):
            tui = Mock()
            self.assertTrue(self.check(result, tui))
            tui.info2.assert_not_called()
            tui.warning.assert_called_once()

    def test_interrupt_retains_original(self):
        with patch.object(engine, "_probe_rtsp_attempt", side_effect=KeyboardInterrupt):
            channels, interrupted = engine._discover_rtsp_channels(attempt(), argparse.Namespace(timeout=2), Mock())
        self.assertTrue(interrupted)
        self.assertEqual([c.channel for c in channels], [1])


if __name__ == "__main__":
    unittest.main()
