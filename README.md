# QuickFill

A lightweight Windows utility that shows a popup menu of configurable buttons and types
the associated text into whatever window currently has focus. Handy for repeatedly
entering logins, tokens, connection strings, and other values.

## How It Works

1. The app starts hidden and listens for a global hotkey.
2. Press the hotkey to open a popup menu of buttons at the mouse cursor.
3. Click a button — the menu closes, focus returns to the previously active window,
   and the button's value is typed out with `pyautogui`.
4. Every entry has a `⧉` copy icon beside it: clicking the label types the value, while
   clicking the icon copies it to the clipboard instead, for windows that ignore
   synthetic keystrokes but still accept a paste.
5. The menu closes on **Escape**, on **Close**, or when it loses focus.

## Requirements

- Windows (the editor fallback is `notepad.exe`; global hotkey/menu behaviour is tuned
  for Windows)
- Python 3 with the packages in `requirements.txt`:

```text
pynput
pyautogui
dotenv
```

Install with:

```bat
pip install -r requirements.txt
```

## Setup

1. Create a virtual environment (the provided launcher expects `.venv`):

   ```bat
   python -m venv .venv
   .venv\Scripts\activate.bat
   pip install -r requirements.txt
   ```

2. `buttons.json` is created automatically on the first run, filled with a few example
   buttons. Edit them in the manager or see the format below.
3. Optionally create a `.env` file to override defaults.
4. Run the app with `run.bat`, or directly:

   ```bat
   pythonw quick_fill.py
   ```

   `pythonw` launches without a console window. Use `python quick_fill.py` if you want
   to see the log output.

## Version Control

The repository ships a `.gitignore` that keeps personal files out of Git. In
particular `buttons.json` and `buttons.json.bak` are ignored, because they hold your own
values (and a `.bak` can still contain the plaintext of a secret from before it was
encrypted), plus `.env` and `quick_fill.lnk`. Commit `buttons.json` yourself only if you
are sure it contains nothing private.

## Stopping the App

Because `pythonw` has no console, there is nothing to `Ctrl + C` in. Stop QuickFill with
any of these:

| Action | Result |
| --- | --- |
| `Ctrl + Shift + Q` | Exits QuickFill from anywhere, menu open or not |
| `Quit` button | Same, from the popup menu |
| Task Manager / `taskkill` | Fallback if the app is unresponsive; the process is `pythonw.exe` running `quick_fill.py` |

`Close` and `Escape` only dismiss the menu — the app keeps listening for the hotkey.

## Usage

| Action | Result |
| --- | --- |
| `Ctrl + Shift + Z` | Open the popup menu at the cursor |
| Click a button | Types that button's value into the focused window |
| Click the `⧉` icon | Copies that button's value to the clipboard instead — paste it with `Ctrl + V` |
| `Manage…` button | Opens the GUI editor for button objects (see below) |
| `Raw File…` button | Opens `buttons.json` in your editor and reloads it on close |
| `Close` button | Dismisses the menu |
| `Escape` | Dismisses the menu |
| Click elsewhere | Menu closes automatically (loses focus) |
| `Quit` button | Exits QuickFill completely |
| `Ctrl + Shift + Q` | Exits QuickFill completely |

Hovering a button shows its configured tooltip in a small yellow popup.

## Managing Buttons in the GUI

Press `Ctrl + Shift + Z`, then click **Manage…**. The manager lists every configured
button by its label — sensitive entries are marked `[sensitive]`, and values are never
displayed — and offers:

| Action | Result |
| --- | --- |
| `Add…` | Opens a form for a new button: `text`, `value`, `sensitive`, `tooltip` |
| `Edit…` | Opens the same form pre-filled for the selected button (double-click also works) |
| `Delete` | Removes the selected button after a confirmation prompt |
| `Save` | Writes the changes to `buttons.json` and reloads the running app |
| `Close` | Closes the manager, asking first when there are unsaved changes |

Notes:

- Edits are held in memory until **Save**, so an accidental change can be discarded.
- The **Value** field is masked (with a **Show** toggle) while **Sensitive** is checked.
- **Press Enter after typing** stores a trailing `\n` in the value, which is how a
  password field gets its Enter keypress. The newline is dropped when the value is copied
  with the `⧉` icon, because a paste cannot press Enter.
- Renaming a button is done by editing its **text** field.
- Saving rewrites each object in the canonical field order `text`, `value`, `sensitive`,
  `tooltip`. Unknown keys in a hand-edited entry are preserved.
- Checking **Sensitive** encrypts the value in `buttons.json` (see
  [Encrypted Sensitive Values](#encrypted-sensitive-values)). The value is only encrypted
  on disk: the form, typing and the `⧉` copy always use the real text.
- The previous version of the file is copied to `buttons.json.bak` on every save, so a
  bad edit can be rolled back by hand.
- Saving an empty list is allowed but warns, because the app exits at startup when there
  are no buttons configured.

An `[sensitive]` entry whose value cannot be decrypted is shown as
`[sensitive] [cannot decrypt]` and its value is treated as empty, so the ciphertext is
never typed into a window. Type the value again with **Edit** and press **Save**.

The **Raw File…** button still opens `buttons.json` in your editor for anything the GUI
cannot handle, such as a file that is not valid JSON. If the manager is open when you
close the editor, it reloads the file from disk.

## Configuration

### `buttons.json`

The app reads a JSON array of button objects. The file path defaults to `buttons.json`
and can be overridden with the `BUTTON_CONFIG_FILE` environment variable.

If the file does not exist when the app starts, it is created with four example buttons:
`My Email`, `Today's Date`, `Username + Enter` and `My Password`. They are only a starting
point showing the field format, a dynamic value, a trailing newline that presses Enter,
and `sensitive` values, so replace them with your own. Nothing is overwritten: an
existing file is never touched.

| Field | Required | Description |
| --- | --- | --- |
| `text` | No | Label shown on the button (defaults to `Unnamed Button`) |
| `value` | No | Text typed when clicked (defaults to empty). Encrypted when `sensitive` is `true` |
| `tooltip` | No | Hover text; if omitted, no tooltip is bound |
| `sensitive` | No | When `true`, the value is encrypted in this file and masked in console output (`********`) |

Example:

```json
[
  {
    "text": "My Email",
    "value": "user@example.com",
    "tooltip": "Inserts your primary email address"
  },
  {
    "text": "My Password",
    "value": "your-password-here\n",
    "sensitive": true,
    "tooltip": "Inserts your password (use with caution!)"
  },
  {
    "text": "Today",
    "value": "{{CURRENT_DATE}}",
    "tooltip": "Inserts today's date"
  }
]
```

Write values in plain text, exactly as you want them typed. A `sensitive` entry is
turned into an encrypted value the next time the file is written, so after using the GUI
or starting the app the same entry looks like this:

```json
  {
    "text": "My Password",
    "value": "enc:v1:AQAAANCMnd8BFdERjHoAwE/Cl+sBAAAA...",
    "sensitive": true,
    "tooltip": "Inserts your password (use with caution!)"
  }
```

### Encrypted Sensitive Values

When `sensitive` is `true` QuickFill encrypts the value before writing it to
`buttons.json`, so a password or token is not readable in the file, in
`buttons.json.bak`, or in anything that copies the file (a backup, a sync folder, a
repository).

The value is only encrypted **on disk**. In memory, in the Add/Edit form, when typing
and when using the `⧉` copy icon, the real text is used exactly as before — nothing about
the normal behaviour changes.

Encryption uses the Windows Data Protection API (DPAPI) under the current Windows user
account, and the stored value is prefixed with `enc:v1:`. There is no key file to keep
track of and no new dependency.

The trade-off is that the ciphertext belongs to the user account and machine that created
it:

- Copying `buttons.json` to another PC or another Windows account means its sensitive
  values cannot be decrypted there. They are treated as empty and marked
  `[cannot decrypt]` in the manager until you enter them again.
- Reinstalling Windows or changing the account password in a way that invalidates the
  DPAPI master key has the same effect. Keep the values somewhere safe if that matters.
- The `enc:v1:` prefix is what marks a value as encrypted. If you hand-edit a file, write
  the value in plain text as usual; it is encrypted on the next write.

Existing files are not rewritten until something needs saving. To encrypt the plaintext
secrets already in `buttons.json`, either press **Save** in the manager or let the app do
it once at startup (`ENCRYPT_SENSITIVE_ON_STARTUP`). Either way the previous contents are
kept in `buttons.json.bak`, which after that migration contains the plaintext original —
delete it once you no longer need the rollback copy.

### Dynamic Values

`{{CURRENT_DATE}}` is replaced with today's date in `YYYY-MM-DD` format at click time.
Any other value is typed literally.

> **Tip:** End a value with `\n` to press Enter after typing (common for passwords and
> login fields). Use `\\` for a literal backslash in domain-style usernames, e.g.
> `"devcorp\\jdoe"`.

### Copying Instead of Typing

Every menu entry has a `⧉` icon on its right. Clicking the **label** types the value;
clicking the **icon** puts the value on the clipboard (without a trailing `\n`) and closes
the menu, so you can paste it with `Ctrl + V`. Use the icon for windows that ignore
synthetic keystrokes but still accept a paste.

Because a copied value would otherwise stay readable until something overwrites it,
QuickFill takes it back automatically:

- **15 seconds after the copy** — change this with `CLIPBOARD_CLEAR_SECONDS`.
- **Shortly after you press `Ctrl + V`** (or `Shift + Insert` / `Ctrl + Insert`), so the value
  disappears as soon as it has been used — controlled by `CLIPBOARD_CLEAR_ON_PASTE`.

The clipboard is only cleared while it still contains exactly the value QuickFill put
there. If you copied something else in the meantime, that copy is left untouched.

> **Note:** Windows keeps its own clipboard history (press `Win+V` to open it). QuickFill
> tags its copies so Windows does not record them there and does not sync them to the
> cloud. Values copied before this feature existed are unaffected — remove those from the
> `Win+V` panel, or turn clipboard history off in **Settings → System → Clipboard**.

### `.env` Options

| Variable | Description |
| --- | --- |
| `BUTTON_CONFIG_FILE` | Path to the button config file (default: `buttons.json`) |
| `EDITOR_PATH` | Editor command used by the **Raw File…** button, e.g. `code --wait` |
| `CLIPBOARD_CLEAR_SECONDS` | Seconds before a copied value is cleared again (default: `15`). `0` keeps the value until something replaces it |
| `CLIPBOARD_CLEAR_ON_PASTE` | Clear the clipboard right after a detected `Ctrl + V`, `Shift + Insert` or `Ctrl + Insert` (default: `true`) |
| `CLIPBOARD_EXCLUDE_FROM_HISTORY` | Ask Windows to keep copied values out of the `Win+V` history and the cloud clipboard (default: `true`) |
| `ENCRYPT_SENSITIVE_ON_STARTUP` | Encrypt plaintext sensitive values found in `buttons.json` right after it is loaded (default: `true`). Set to `false` to only encrypt when the manager saves |

**Editor resolution order** for the Raw File… button:

1. `EDITOR_PATH` from `.env`
2. `VISUAL` / `EDITOR` environment variables (non-Windows only)
3. OS default — `notepad.exe` on Windows, `nano` elsewhere

> If you use a GUI editor, pass a wait flag (for example `code --wait`) so the process
> blocks until you close the file; the config is reloaded after the editor exits.

## Security Warning

Values marked `sensitive` are encrypted in `buttons.json` with the Windows Data
Protection API, so the file no longer holds those secrets in readable form. This is
still a convenience tool, not a password manager:

- Anything you store **without** `sensitive` is plain text in `buttons.json`.
- `buttons.json.bak` holds a copy of the file from before the last save. After the first
  encryption it is encrypted too, but the run that migrates an existing file leaves the
  plaintext original in the backup.
- The ciphertext can only be decrypted by the same Windows user on the same machine, so a
  stolen `buttons.json` is not readable elsewhere — but anyone who can run programs as
  that user on that machine can decrypt it, and so can this app.
- `pyautogui` types the real characters into whatever window has focus, and the `⧉` copy
  icon puts the real value on the Windows clipboard. Encryption at rest protects the file,
  not those moments.
- Do not commit `buttons.json` or `.env` to version control. Prefer a real password
  manager for secrets.

The `⧉` copy icon puts the value on the Windows clipboard, where any application can read
it. QuickFill limits that window by clearing the clipboard after 15 seconds or right after
a paste, and by asking Windows not to keep the copy in the `Win+V` history or the cloud
clipboard — but the value is still exposed while it sits there. Use a short
`CLIPBOARD_CLEAR_SECONDS` for anything secret.

## Files

| File | Purpose |
| --- | --- |
| `quick_fill.py` | Main application |
| `button_manager.py` | GUI manager used to add, edit and delete button objects |
| `crypto_store.py` | Windows DPAPI helper that encrypts and decrypts sensitive values |
| `clipboard_win.py` | Win32 clipboard helper (history exclusion, read-back, clear) |
| `buttons.json` | Button definitions |
| `buttons.json.bak` | Copy of `buttons.json` from before the last GUI save (created automatically) |
| `requirements.txt` | Python dependencies |
| `run.bat` | Activates `.venv` and launches with `pythonw` |
| `quick_fill.lnk` | Shortcut for launching the app |
| `.env` | Optional environment overrides (not included) |

## Troubleshooting

| Problem | Fix |
| --- | --- |
| "Application cannot start without button configurations" | `buttons.json` exists but holds no buttons (an empty list), or `BUTTON_CONFIG_FILE` points at the wrong file. Delete it to have the examples recreated |
| "Button configuration file not found" | The file could not be created at startup — check that its folder is writable, or set `BUTTON_CONFIG_FILE` to a writable path |
| Hotkey does nothing | Another app may be capturing `Ctrl + Shift + Z`; ensure the script is running and check the console log |
| The quit hotkey does nothing | Another app may be capturing `Ctrl + Shift + Q`. Use the `Quit` button in the menu, or end the `pythonw.exe` process that runs `quick_fill.py` |
| No window and no way to close it | That is expected — QuickFill is a background hotkey listener. See [Stopping the App](#stopping-the-app) |
| Text goes to the wrong window | The menu needs focus to close first; keep the 50 ms delay in `type_text` or trigger from a stable window |
| Config button opens nothing | Set `EDITOR_PATH` in `.env`, e.g. `EDITOR_PATH=C:\path\to\editor.exe` |
| `Manage…` shows an error instead of opening | `buttons.json` is missing, is not a JSON list, or contains invalid JSON — repair it with `Raw File…` |
| Manager reports "Could not save" | `buttons.json` is open in another program (for example Notepad); close it and save again |
| A GUI edit is missing after restart | The change was never saved — the manager only writes the file on `Save` |
| Need to undo a bad save | Restore `buttons.json.bak` over `buttons.json` |
| Special characters not typed | `pyautogui.write` handles ASCII only. Click the `⧉` icon next to the entry and paste the value manually |
| A particular app ignores the typing entirely | The app blocks synthetic keystrokes. Use the `⧉` icon and paste with `Ctrl + V` |
| Clipboard is empty right after clicking `⧉` | The auto-clear fired. Raise `CLIPBOARD_CLEAR_SECONDS`, or set `CLIPBOARD_CLEAR_ON_PASTE=false` if the paste detector clears too early |
| A copied value is still listed in `Win+V` | Older copies are not purged retroactively. Confirm `CLIPBOARD_EXCLUDE_FROM_HISTORY=true` (the default), copy again, and remove the old entry from the `Win+V` panel |
