#!/usr/bin/env python3
"""Approve Turris OS updates from Telegram.

Runs on the router. When the Turris updater waits for an approval, the bot sends the plan to one Telegram chat
with the buttons Approve and Deny, and resolves the request through the updater's own API (svupdater.approvals),
the same one reForis uses. It only polls Telegram, so the router needs no open port.

Only the Python standard library and svupdater (part of Turris OS) are used.
"""

import argparse
import html
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

__version__ = "0.1.0"

LOG = logging.getLogger("turris-update-approver")

DEFAULT_CONFIG = "/etc/turris-update-approver.json"
DEFAULT_STATE = "/var/run/turris-update-approver.json"

# Telegram limits callback data to 64 bytes, the approval hash is longer. A prefix identifies the request,
# the full hash is taken from the updater when the button is pressed.
HASH_PREFIX_LENGTH = 32
# Telegram allows 4096 characters per message.
MAX_PACKAGES_LISTED = 40
# Shown first in the package list.
NOTABLE_PACKAGES = ("turris-version", "kernel", "base-files", "procd", "netifd", "firewall4", "dnsmasq-full",
                    "openssh-server", "hostapd", "wpad", "busybox", "libc")


class TelegramError(Exception):
    pass


class Telegram:
    """Minimal Telegram Bot API client."""

    def __init__(self, token, api_url="https://api.telegram.org"):
        self._base = f"{api_url}/bot{token}/"

    def call(self, method, http_timeout=30, **params):
        data = json.dumps(params).encode()
        request = urllib.request.Request(self._base + method, data=data,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=http_timeout + 10) as response:
                body = json.load(response)
        except urllib.error.HTTPError as e:
            try:
                body = json.load(e)
            except ValueError:
                raise TelegramError(f"{method}: HTTP {e.code}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method}: {body.get('description', 'unknown error')}")
        return body["result"]


class Approvals:
    """The updater's approval API (svupdater.approvals), imported on first use."""

    def __init__(self):
        from svupdater import approvals
        self._approvals = approvals

    def current(self):
        return self._approvals.current()

    def approve(self, approval_hash):
        self._approvals.approve(approval_hash)

    def deny(self, approval_hash):
        self._approvals.deny(approval_hash)


def run_updater():
    """Start the updater in the background, so an approved plan is installed now instead of at its next run."""
    subprocess.Popen(["updater-supervisor", "--daemon"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _sort_key(package):
    name = package.get("name", "")
    notable = [i for i, prefix in enumerate(NOTABLE_PACKAGES) if name == prefix or name.startswith(prefix + "-")]
    return (notable[0] if notable else len(NOTABLE_PACKAGES), name)


def _describe(package):
    name = html.escape(package.get("name", "?"))
    cur, new = package.get("cur_ver"), package.get("new_ver")
    op = package.get("op", "")
    if cur and new:
        return f"{name} {html.escape(cur)} → {html.escape(new)}"
    if new:
        return f"{name} {html.escape(new)} ({html.escape(op or 'install')})"
    if cur:
        return f"{name} {html.escape(cur)} ({html.escape(op or 'remove')})"
    return f"{name} ({html.escape(op)})"


def format_request(request, hostname):
    """Text of the message for a pending approval request (HTML parse mode)."""
    plan = sorted(request.get("plan") or [], key=_sort_key)
    lines = [f"<b>Update for {html.escape(hostname)} waiting for approval</b>"]
    version = next((p for p in plan if p.get("name") == "turris-version"), None)
    if version and version.get("cur_ver") and version.get("new_ver"):
        lines.append(f"Turris OS {html.escape(version['cur_ver'])} → {html.escape(version['new_ver'])}")
    counts = {}
    for package in plan:
        counts[package.get("op", "?")] = counts.get(package.get("op", "?"), 0) + 1
    summary = ", ".join(f"{n} {html.escape(op)}" for op, n in sorted(counts.items()))
    lines.append(f"{len(plan)} packages: {summary}" if plan else "Empty plan")
    if request.get("reboot"):
        lines.append("Needs a reboot afterwards.")
    if request.get("time"):
        lines.append("Planned " + time.strftime("%Y-%m-%d %H:%M", time.localtime(request["time"])))
    lines.append("")
    lines += [_describe(p) for p in plan[:MAX_PACKAGES_LISTED]]
    if len(plan) > MAX_PACKAGES_LISTED:
        lines.append(f"… and {len(plan) - MAX_PACKAGES_LISTED} more")
    return "\n".join(lines)


def buttons(approval_hash):
    prefix = approval_hash[:HASH_PREFIX_LENGTH]
    return {"inline_keyboard": [[{"text": "Approve", "callback_data": f"approve:{prefix}"},
                                 {"text": "Deny", "callback_data": f"deny:{prefix}"}]]}


class Approver:

    def __init__(self, telegram, approvals, chat_id, hostname, state_file, run_after_approve=True,
                 allowed_user_ids=None, start_updater=run_updater):
        self.telegram = telegram
        self.approvals = approvals
        self.chat_id = int(chat_id)
        # In a private chat the chat id is the user id. In a group, list who may press the buttons.
        self.allowed_user_ids = {int(u) for u in allowed_user_ids} if allowed_user_ids else {self.chat_id}
        self.hostname = hostname
        self.state_file = state_file
        self.run_after_approve = run_after_approve
        self.start_updater = start_updater
        # The request a message was sent for: {"hash": ..., "message_id": ..., "text": ...}
        self.state = self._load_state()

    def _load_state(self):
        try:
            with open(self.state_file) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_state(self):
        tmp = self.state_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.state_file)

    def _close_message(self, note):
        """Remove the buttons from the last message and append a note."""
        if not self.state.get("message_id"):
            return
        try:
            self.telegram.call("editMessageText", chat_id=self.chat_id, message_id=self.state["message_id"],
                               text=self.state.get("text", "") + f"\n\n<b>{html.escape(note)}</b>",
                               parse_mode="HTML")
        except TelegramError as e:
            LOG.warning("could not update the message: %s", e)
        self.state = {}
        self._save_state()

    def check(self):
        """Send a message for a new pending request, close the message of a request that is gone."""
        request = self.approvals.current()
        pending = request if request and request.get("status") == "asked" else None
        known = self.state.get("hash")

        if known and (pending is None or pending["hash"] != known):
            if request and request.get("hash") == known:
                self._close_message(f"{request.get('status', 'resolved').capitalize()} outside of Telegram.")
            else:
                self._close_message("No longer pending: installed or replaced by a newer plan.")

        if pending and pending["hash"] != self.state.get("hash"):
            text = format_request(pending, self.hostname)
            message = self.telegram.call("sendMessage", chat_id=self.chat_id, text=text, parse_mode="HTML",
                                         reply_markup=buttons(pending["hash"]))
            self.state = {"hash": pending["hash"], "message_id": message["message_id"], "text": text}
            self._save_state()
            LOG.info("asked for approval of %s", pending["hash"])

    def handle_update(self, update):
        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
        elif "message" in update:
            self._handle_message(update["message"])

    def _handle_message(self, message):
        chat_id = message.get("chat", {}).get("id")
        if chat_id != self.chat_id:
            LOG.warning("ignored a message from chat %s", chat_id)
            return
        if message.get("text", "").split("@")[0].strip() in ("/status", "/start"):
            request = self.approvals.current()
            if request and request.get("status") == "asked":
                # Send the request again, with buttons.
                self.state = {}
                self.check()
            else:
                status = request.get("status") if request else None
                text = f"No update of {self.hostname} is waiting for approval."
                if status:
                    text += f" The last request is {status}."
                self.telegram.call("sendMessage", chat_id=self.chat_id, text=text)

    def _handle_callback(self, query):
        user_id = query.get("from", {}).get("id")
        message = query.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        if chat_id != self.chat_id or user_id not in self.allowed_user_ids:
            LOG.warning("refused a button press of user %s in chat %s", user_id, chat_id)
            self._answer(query, "Not allowed.")
            return

        action, _, prefix = query.get("data", "").partition(":")
        request = self.approvals.current()
        if action not in ("approve", "deny") or len(prefix) != HASH_PREFIX_LENGTH \
                or not request or request.get("status") != "asked" or not request["hash"].startswith(prefix):
            self._answer(query, "This request is no longer pending.")
            self._remove_buttons(chat_id, message.get("message_id"))
            return

        try:
            if action == "approve":
                self.approvals.approve(request["hash"])
            else:
                self.approvals.deny(request["hash"])
        except Exception as e:  # svupdater raises UpdaterApproveInvalidError when the plan changed meanwhile
            LOG.warning("%s of %s failed: %s", action, request["hash"], e)
            self._answer(query, f"Failed: {e or type(e).__name__}")
            return

        LOG.info("%s: %s", "approved" if action == "approve" else "denied", request["hash"])
        note = "Approved" if action == "approve" else "Denied"
        if action == "approve" and self.run_after_approve:
            try:
                self.start_updater()
                note += ", the update is running now"
            except OSError as e:
                LOG.warning("could not start the updater: %s", e)
                note += ", it is installed at the updater's next run"
        elif action == "approve":
            note += ", it is installed at the updater's next run"
        self._answer(query, note)
        if self.state.get("hash") == request["hash"]:
            self._close_message(note + ".")
        else:
            self._remove_buttons(chat_id, message.get("message_id"))

    def _answer(self, query, text):
        try:
            self.telegram.call("answerCallbackQuery", callback_query_id=query.get("id"), text=text)
        except TelegramError as e:
            LOG.warning("could not answer the button press: %s", e)

    def _remove_buttons(self, chat_id, message_id):
        if not message_id:
            return
        try:
            self.telegram.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                               reply_markup={"inline_keyboard": []})
        except TelegramError as e:
            LOG.warning("could not remove the buttons: %s", e)


def serve(approver, telegram, check_interval, sleep=time.sleep, clock=time.monotonic, rounds=None):
    """Poll Telegram and check the updater every check_interval seconds. rounds limits the loop for tests."""
    offset = None
    next_check = 0.0
    while rounds is None or rounds > 0:
        if rounds is not None:
            rounds -= 1
        try:
            if clock() >= next_check:
                approver.check()
                next_check = clock() + check_interval
            wait = max(1, min(50, int(next_check - clock())))
            params = {"timeout": wait, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            for update in telegram.call("getUpdates", http_timeout=wait, **params):
                offset = update["update_id"] + 1
                approver.handle_update(update)
        except (TelegramError, OSError, ValueError) as e:
            # No internet yet after a boot, or Telegram is down: try again later.
            LOG.warning("%s, retrying in 60 s", e)
            sleep(60)


def load_config(path):
    with open(path) as f:
        config = json.load(f)
    missing = [key for key in ("token", "chat_id") if not config.get(key)]
    if missing:
        raise ValueError(f"{path}: missing {', '.join(missing)}")
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"config file (default {DEFAULT_CONFIG})")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s", stream=sys.stderr)
    try:
        config = load_config(args.config)
    except (OSError, ValueError) as e:
        LOG.error("%s", e)
        return 1

    telegram = Telegram(config["token"], config.get("api_url", "https://api.telegram.org"))
    approver = Approver(telegram, Approvals(), config["chat_id"], config.get("hostname") or os.uname().nodename,
                        config.get("state_file", DEFAULT_STATE), config.get("run_after_approve", True),
                        config.get("allowed_user_ids"))
    LOG.info("started, version %s", __version__)
    serve(approver, telegram, int(config.get("check_interval", 300)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
