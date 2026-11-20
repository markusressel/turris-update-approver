import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import turris_update_approver as tua  # noqa: E402

CHAT = 4242
HASH = "a" * 64
OTHER_HASH = "b" * 64


class UpdaterApproveInvalidError(Exception):
    pass


class FakeApprovals:
    """Behaves like svupdater.approvals: approve and deny only work on the current hash."""

    def __init__(self, request=None):
        self.request = request

    def current(self):
        return self.request

    def _resolve(self, approval_hash, status):
        if not self.request or self.request["hash"] != approval_hash or self.request["status"] != "asked":
            raise UpdaterApproveInvalidError("Not matching hash passed")
        self.request["status"] = status

    def approve(self, approval_hash):
        self._resolve(approval_hash, "granted")

    def deny(self, approval_hash):
        self._resolve(approval_hash, "denied")


class FakeTelegram:

    def __init__(self, updates=None):
        self.calls = []
        self.updates = list(updates or [])
        self.next_message_id = 100

    def call(self, method, http_timeout=30, **params):
        self.calls.append((method, params))
        if method == "sendMessage":
            self.next_message_id += 1
            return {"message_id": self.next_message_id}
        if method == "getUpdates":
            batch, self.updates = self.updates, []
            return batch
        return True

    def methods(self):
        return [m for m, _ in self.calls]

    def last(self, method):
        return [p for m, p in self.calls if m == method][-1]


def request(hsh=HASH, status="asked", plan=None):
    return {"hash": hsh, "status": status, "time": 1795190000, "reboot": "delayed",
            "plan": plan if plan is not None else [
                {"name": "vim", "op": "upgrade", "cur_ver": "9.1", "new_ver": "9.2"},
                {"name": "turris-version", "op": "upgrade", "cur_ver": "9.1.5", "new_ver": "9.1.6"},
                {"name": "kernel", "op": "upgrade", "cur_ver": "6.6.159-r3", "new_ver": "6.6.160-r1"},
                {"name": "htop", "op": "install", "cur_ver": None, "new_ver": "3.3"},
                {"name": "old-thing", "op": "remove", "cur_ver": "1.0", "new_ver": None},
            ]}


def press(action, hsh=HASH, user=CHAT, chat=CHAT, message_id=101):
    return {"update_id": 1, "callback_query": {
        "id": "q1", "from": {"id": user}, "data": f"{action}:{hsh[:tua.HASH_PREFIX_LENGTH]}",
        "message": {"message_id": message_id, "chat": {"id": chat}}}}


class ApproverTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state_file = os.path.join(self.dir.name, "state.json")
        self.telegram = FakeTelegram()
        self.approvals = FakeApprovals()
        self.updater_runs = 0

    def tearDown(self):
        self.dir.cleanup()

    def approver(self, **kwargs):
        def start():
            self.updater_runs += 1
        return tua.Approver(self.telegram, self.approvals, CHAT, "turris", self.state_file,
                            start_updater=start, **kwargs)

    def test_nothing_pending_sends_nothing(self):
        self.approver().check()
        self.assertEqual(self.telegram.calls, [])

    def test_pending_request_is_sent_once_with_buttons(self):
        self.approvals.request = request()
        approver = self.approver()
        approver.check()
        approver.check()
        self.assertEqual(self.telegram.methods(), ["sendMessage"])
        sent = self.telegram.last("sendMessage")
        self.assertEqual(sent["chat_id"], CHAT)
        self.assertIn("Turris OS 9.1.5 → 9.1.6", sent["text"])
        self.assertIn("5 packages", sent["text"])
        self.assertIn("reboot", sent["text"])
        # The important packages come first.
        self.assertLess(sent["text"].index("turris-version"), sent["text"].index("vim"))
        self.assertLess(sent["text"].index("kernel"), sent["text"].index("htop"))
        data = [b["callback_data"] for b in sent["reply_markup"]["inline_keyboard"][0]]
        self.assertEqual(data, [f"approve:{HASH[:32]}", f"deny:{HASH[:32]}"])
        self.assertTrue(all(len(d.encode()) <= 64 for d in data))

    def test_state_survives_a_restart(self):
        self.approvals.request = request()
        self.approver().check()
        self.approver().check()
        self.assertEqual(self.telegram.methods(), ["sendMessage"])

    def test_approve_resolves_and_starts_the_updater(self):
        self.approvals.request = request()
        approver = self.approver()
        approver.check()
        approver.handle_update(press("approve"))
        self.assertEqual(self.approvals.request["status"], "granted")
        self.assertEqual(self.updater_runs, 1)
        self.assertIn("Approved", self.telegram.last("answerCallbackQuery")["text"])
        edit = self.telegram.last("editMessageText")
        self.assertNotIn("reply_markup", edit)
        self.assertIn("Approved, the update is running now.", edit["text"])
        self.assertEqual(approver.state, {})

    def test_approve_without_running_the_updater(self):
        self.approvals.request = request()
        approver = self.approver(run_after_approve=False)
        approver.check()
        approver.handle_update(press("approve"))
        self.assertEqual(self.updater_runs, 0)
        self.assertIn("next run", self.telegram.last("answerCallbackQuery")["text"])

    def test_deny(self):
        self.approvals.request = request()
        approver = self.approver()
        approver.check()
        approver.handle_update(press("deny"))
        self.assertEqual(self.approvals.request["status"], "denied")
        self.assertEqual(self.updater_runs, 0)

    def test_stranger_cannot_approve(self):
        self.approvals.request = request()
        approver = self.approver()
        approver.check()
        approver.handle_update(press("approve", user=999))
        approver.handle_update(press("approve", chat=999, user=999))
        self.assertEqual(self.approvals.request["status"], "asked")
        self.assertEqual(self.telegram.last("answerCallbackQuery")["text"], "Not allowed.")

    def test_group_member_listed_may_approve(self):
        group = -100
        self.approvals.request = request()
        approver = tua.Approver(self.telegram, self.approvals, group, "turris", self.state_file,
                                allowed_user_ids=[7], start_updater=lambda: None)
        approver.handle_update(press("approve", user=8, chat=group))
        self.assertEqual(self.approvals.request["status"], "asked")
        approver.handle_update(press("approve", user=7, chat=group))
        self.assertEqual(self.approvals.request["status"], "granted")

    def test_button_of_an_old_plan_does_nothing(self):
        self.approvals.request = request()
        approver = self.approver()
        approver.check()
        self.approvals.request = request(OTHER_HASH)
        approver.handle_update(press("approve", hsh=HASH))
        self.assertEqual(self.approvals.request["status"], "asked")
        self.assertIn("no longer pending", self.telegram.last("answerCallbackQuery")["text"])
        self.assertIn("editMessageReplyMarkup", self.telegram.methods())

    def test_failed_approve_is_reported(self):
        class Racing(FakeApprovals):
            def approve(self, approval_hash):
                raise UpdaterApproveInvalidError("Not matching hash passed")
        self.approvals = Racing(request())
        approver = self.approver()
        approver.handle_update(press("approve"))
        self.assertIn("Not matching hash passed", self.telegram.last("answerCallbackQuery")["text"])
        self.assertEqual(self.updater_runs, 0)

    def test_malformed_button_data(self):
        self.approvals.request = request()
        approver = self.approver()
        update = press("approve")
        update["callback_query"]["data"] = "approve:" + "a" * 5
        approver.handle_update(update)
        update["callback_query"]["data"] = "reboot:" + HASH[:32]
        approver.handle_update(update)
        self.assertEqual(self.approvals.request["status"], "asked")

    def test_resolved_in_reforis_closes_the_message(self):
        self.approvals.request = request()
        approver = self.approver()
        approver.check()
        self.approvals.request["status"] = "granted"
        approver.check()
        self.assertIn("Granted outside of Telegram.", self.telegram.last("editMessageText")["text"])
        self.assertEqual(approver.state, {})

    def test_newer_plan_replaces_the_message(self):
        self.approvals.request = request()
        approver = self.approver()
        approver.check()
        self.approvals.request = request(OTHER_HASH)
        approver.check()
        self.assertEqual(self.telegram.methods(), ["sendMessage", "editMessageText", "sendMessage"])
        self.assertIn("No longer pending", self.telegram.calls[1][1]["text"])
        self.assertEqual(approver.state["hash"], OTHER_HASH)

    def test_installed_plan_closes_the_message(self):
        self.approvals.request = request()
        approver = self.approver()
        approver.check()
        self.approvals.request = None
        approver.check()
        self.assertIn("No longer pending", self.telegram.last("editMessageText")["text"])

    def test_status_command(self):
        approver = self.approver()
        approver.handle_update({"update_id": 1, "message": {"chat": {"id": CHAT}, "text": "/status"}})
        self.assertIn("No update of turris is waiting", self.telegram.last("sendMessage")["text"])
        self.approvals.request = request()
        approver.check()
        approver.handle_update({"update_id": 2, "message": {"chat": {"id": CHAT}, "text": "/status"}})
        self.assertEqual(self.telegram.methods().count("sendMessage"), 3)
        self.assertIn("reply_markup", self.telegram.last("sendMessage"))

    def test_commands_from_other_chats_are_ignored(self):
        self.approver().handle_update({"update_id": 1, "message": {"chat": {"id": 999}, "text": "/status"}})
        self.assertEqual(self.telegram.calls, [])

    def test_html_is_escaped(self):
        text = tua.format_request(request(plan=[{"name": "<b>x", "op": "install", "cur_ver": None,
                                                 "new_ver": "1&2"}]), "host<1>")
        self.assertIn("&lt;b&gt;x", text)
        self.assertIn("1&amp;2", text)
        self.assertIn("host&lt;1&gt;", text)

    def test_long_plans_are_cut(self):
        plan = [{"name": f"pkg{i}", "op": "upgrade", "cur_ver": "1", "new_ver": "2"} for i in range(500)]
        text = tua.format_request(request(plan=plan), "turris")
        self.assertIn("… and 460 more", text)
        self.assertLess(len(text), 4096)


class ServeTest(unittest.TestCase):

    def test_polls_and_handles_updates(self):
        with tempfile.TemporaryDirectory() as d:
            telegram = FakeTelegram([press("approve")])
            approvals = FakeApprovals(request())
            approver = tua.Approver(telegram, approvals, CHAT, "turris", os.path.join(d, "s.json"),
                                    start_updater=lambda: None)
            tua.serve(approver, telegram, check_interval=300, sleep=lambda s: None, rounds=2)
            self.assertEqual(approvals.request["status"], "granted")
            polls = [p for m, p in telegram.calls if m == "getUpdates"]
            self.assertNotIn("offset", polls[0])
            self.assertEqual(polls[1]["offset"], 2)
            self.assertLessEqual(polls[0]["timeout"], 50)

    def test_network_errors_do_not_stop_the_loop(self):
        class Offline(FakeTelegram):
            def call(self, method, http_timeout=30, **params):
                raise OSError("Network unreachable")
        slept = []
        approver = tua.Approver(FakeTelegram(), FakeApprovals(request()), CHAT, "turris",
                                os.devnull + ".x", start_updater=lambda: None)
        approver.telegram = Offline()
        approver._save_state = lambda: None
        tua.serve(approver, approver.telegram, 300, sleep=slept.append, rounds=3)
        self.assertEqual(slept, [60, 60, 60])

    def test_timeouts_poll_again_without_waiting(self):
        class Slow(FakeTelegram):
            def call(self, method, http_timeout=30, **params):
                raise TimeoutError("The read operation timed out")
        slept = []
        approver = tua.Approver(FakeTelegram(), FakeApprovals(), CHAT, "turris",
                                os.devnull + ".x", start_updater=lambda: None)
        tua.serve(approver, Slow(), 300, sleep=slept.append, rounds=3)
        self.assertEqual(slept, [])


class ConfigTest(unittest.TestCase):

    def test_missing_keys(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"token": "x"}, f)
        try:
            with self.assertRaises(ValueError):
                tua.load_config(f.name)
        finally:
            os.unlink(f.name)


class TelegramClientTest(unittest.TestCase):
    """The real client against a local HTTP server."""

    def setUp(self):
        import http.server
        import threading
        received = self.received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append((self.path, body))
                if self.path.endswith("/fail"):
                    status, reply = 400, {"ok": False, "description": "Bad Request: chat not found"}
                else:
                    status, reply = 200, {"ok": True, "result": {"echo": body}}
                data = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.telegram = tua.Telegram("123:abc", f"http://127.0.0.1:{self.server.server_port}")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_call_sends_json_and_returns_result(self):
        result = self.telegram.call("getUpdates", http_timeout=1, timeout=1, offset=5)
        self.assertEqual(result, {"echo": {"timeout": 1, "offset": 5}})
        self.assertEqual(self.received[0][0], "/bot123:abc/getUpdates")

    def test_api_error_raises(self):
        with self.assertRaisesRegex(tua.TelegramError, "chat not found"):
            self.telegram.call("fail")


if __name__ == "__main__":
    unittest.main()
