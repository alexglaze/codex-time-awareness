# Time Awareness for Codex

Pick up a chat after hours or days away and give Codex the timing context automatically.

This plugin adds the current date/time and elapsed time since the chat's last recorded activity at each message. It also refreshes time context when you reopen a chat or its context is compacted.

## Requirements

- Local Codex desktop or CLI with lifecycle hooks enabled. Native Windows requires a build supporting the documented `commandWindows` hook override.
- Python 3.9+: available as `python3` on macOS/Linux, or `python` on Windows.
- No network service, API key, or Python packages for the default local clock.

Windows, macOS, and Linux are supported. Cloud-orchestrated chats are not supported.

### Windows setup

Install Python 3.9 or newer from [python.org](https://www.python.org/downloads/windows/) with `python` available on PATH. In a new terminal, confirm `python --version` reports the installed interpreter rather than opening the Microsoft Store. Restart Codex after changing PATH so its hooks inherit the new environment.

The plugin uses the Windows local timezone by default, without extra packages. If you configure an IANA timezone such as `America/New_York`, install its data for the same interpreter:

```powershell
python -m pip install tzdata
```

If named timezone data is missing, the hook still provides the computer's local clock and reports that fallback. In WSL, install and run the plugin within WSL using the Linux requirements.

## Install

```sh
codex plugin marketplace add alexglaze/codex-time-awareness
codex plugin add time-awareness@time-awareness-community
```

Open **Settings > Hooks** in Codex desktop, or `/hooks` in the CLI. Review and trust the four Time Awareness hooks. Reopen your chat; if the source or hooks are not listed yet, restart the app. The plugin source is named **Time Awareness** in the plugin menu.

For a fixed version, add the marketplace with `--ref v0.2.0`. To update an unpinned installation later, run `codex plugin marketplace upgrade time-awareness-community`, then `codex plugin add time-awareness@time-awareness-community`. Review changed hooks if Codex requests it.

### Install from a ZIP instead

Download the ZIP from [Releases](https://github.com/alexglaze/codex-time-awareness/releases), unzip it, then run these commands, replacing the folder path with the extracted folder:

```sh
codex plugin marketplace add /absolute/path/to/time-awareness-plugin
codex plugin add time-awareness@time-awareness-community
```

On Windows, use a quoted Windows path such as `codex plugin marketplace add "C:\Tools\time-awareness-plugin"` for the first command.

Trust the hooks as described above. Keep the extracted folder at that path: it is the source of the local marketplace.

If you installed the earlier config-only version, remove its four registrations first using that version's `install.py --remove` to avoid duplicate context. Installing a plugin does not transfer timing history from the config-only version.

## What it does

- Records message submission, assistant turn completion, and interruption times separately for each chat.
- Reports the current local time, UTC, and elapsed time; uses UTC arithmetic across daylight-saving changes.
- Uses the computer's timezone by default. An optional `settings.json` in the plugin's writable `PLUGIN_DATA` directory can contain `{"timezone": "America/New_York"}` or another IANA timezone.
- Stores small timestamp records under `PLUGIN_DATA/state`, outside the cached plugin package.
- Tells Codex to use timing silently unless it matters, and to preserve the meaning of historical relative dates such as “today.”

On first use, earlier timing is unknown. A new/forked chat has independent records. If the last completion was not recorded, the hook reports time since the previous message; this can include active work. A clock snapshot does not establish what the user did during the gap. The clock does not continuously refresh during a long response.

## Privacy

The hook receives the standard Codex event payload but does not save or echo prompt/reply content or read transcripts. It stores timestamps, event names, and a turn identifier. Chat identifiers are hashed for filenames. There are no outbound network requests. Missing/corrupt state does not block prompts or restart assistant turns.

## Distribution status

This is an independent community plugin distributed through this GitHub repository and a custom marketplace source. It is not an official OpenAI plugin or a listing in the default public Plugin Directory.

[Plugin packaging and local marketplaces](https://developers.openai.com/plugins/build/plugins) · [Public submission restrictions](https://developers.openai.com/plugins/deploy/submission) · [Hook review and behavior](https://learn.chatgpt.com/docs/hooks)

## Remove

```sh
codex plugin remove time-awareness@time-awareness-community
codex plugin marketplace remove time-awareness-community
```

## Development checks

```sh
python3 -B -m unittest discover -s tests -v
```

The package uses the supported `.codex-plugin/plugin.json` compatibility layout. Tests cover time gaps, DST, chat isolation, interruptions, delayed events, safe state recovery, plugin data storage, config installation, process locking, Windows hook commands, Unicode paths, and operation without timezone packages. CI runs on Windows, macOS, and Linux.

Current-time context from the config-only installation has also been observed in a live Codex chat. The plugin's hook registration and command execution are checked separately; every installation still requires the user's hook trust review.

## License and contributions

[MIT](LICENSE), copyright Alex Glaze. Bug reports and pull requests are welcome. Include your Codex version, operating system, and a synthetic reproduction; avoid sharing private chat content or timestamp records.
