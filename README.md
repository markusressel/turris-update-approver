# turris-update-approver

Approve updates of a [Turris](https://www.turris.com/) router from Telegram.

With update approvals enabled, the Turris updater plans each update and waits until someone approves it in
reForis. turris-update-approver sends that plan to your Telegram chat, with the buttons **Approve** and **Deny**:

```
Update for turris waiting for approval
Turris OS 9.1.5 → 9.1.6
42 packages: 1 install, 41 upgrade
Needs a reboot afterwards.
Planned 2026-11-21 04:12

turris-version 9.1.5 → 9.1.6
kernel 6.6.159-r3 → 6.6.160-r1
...
```

**Approve** grants the request and starts the updater right away, **Deny** rejects it. Both go through the
updater's own API (`svupdater.approvals`), the one reForis uses, and only for the exact plan in the message: when
the updater has planned something else in the meantime, the old buttons do nothing. A request resolved in reForis,
installed or replaced by a newer plan loses its buttons in the chat.

It runs on the router and only polls Telegram, so the router needs no open port. It is one Python file that uses
nothing but the standard library and `svupdater`, which every Turris OS has.

Tested on a Turris Omnia with Turris OS 9.1 (Python 3.11).

## Security

Whoever may press the buttons decides what is installed on your router as root.

- Only button presses from the configured chat count, and in it only from the allowed users. In a private chat
  that is you alone. Others get "Not allowed." and are logged.
- The bot can approve or deny the pending plan. It runs no other commands, and the plan itself comes from the
  updater, not from Telegram.
- The token lets anyone send messages as the bot, and read the updates it has not fetched yet. Keep the config
  file readable for root only.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and note its token. Use a bot of its own:
   two programs cannot fetch the updates of the same bot.
2. Send the bot a message, then find your chat id with
   `curl -s https://api.telegram.org/bot<token>/getUpdates` (`message.chat.id`).
3. Enable approvals on the router (reForis: Package Management → Updates, "Update approval needed", without
   delayed approval), or with `uci`:

    ```sh
    uci set updater.autorun.enable='1'
    uci set updater.autorun.approvals='1'
    uci -q delete updater.autorun.auto_approve_time
    uci commit updater
    ```

4. Install it on the router as root:

    ```sh
    VERSION=v0.1.0
    BASE=https://raw.githubusercontent.com/markusressel/turris-update-approver/$VERSION
    wget -O /usr/bin/turris-update-approver $BASE/turris_update_approver.py
    wget -O /etc/init.d/turris-update-approver $BASE/files/turris-update-approver.init
    chmod 755 /usr/bin/turris-update-approver /etc/init.d/turris-update-approver
    ```

5. Write `/etc/turris-update-approver.json` (see [the example](files/turris-update-approver.json.example)) and
   make it readable for root only:

    ```sh
    chmod 600 /etc/turris-update-approver.json
    /etc/init.d/turris-update-approver enable
    /etc/init.d/turris-update-approver start
    logread -e turris-update-approver
    ```

Send `/status` to the bot to see whether an update is waiting.

## Configuration

`/etc/turris-update-approver.json`:

| Key | Default | Meaning |
|---|---|---|
| `token` | | Bot token from BotFather. |
| `chat_id` | | Chat that gets the messages. Your user id for a private chat. |
| `allowed_user_ids` | `[chat_id]` | Users who may press the buttons. Needed when `chat_id` is a group. |
| `check_interval` | `300` | Seconds between two looks at the updater. |
| `run_after_approve` | `true` | Start the updater after Approve. With `false` the update waits for the updater's next scheduled run. |
| `hostname` | the router's hostname | Name shown in the messages. |
| `state_file` | `/var/run/turris-update-approver.json` | Remembers the last message, so a restart does not send it again. |

## Development

```sh
python3 -m unittest discover -s tests
```

The tests replace `svupdater` and Telegram with fakes, the HTTP client runs against a local server.

## License

[GNU AGPL v3](LICENSE)
