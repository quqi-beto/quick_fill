import tkinter as tk
from tkinter import messagebox
from pynput import keyboard
import pyautogui
import threading
import time
import dotenv
import os
from datetime import datetime
import subprocess
import sys

import clipboard_win
import crypto_store
from button_manager import (
    ConfigError,
    count_decrypt_failures,
    ensure_button_config_exists,
    get_button_manager,
    load_button_config,
    migrate_plaintext_sensitive_values,
    open_button_manager,
    read_button_config,
    split_trailing_newline,
)


def _env_int(name, default):
    """Reads a non-negative integer from the environment, falling back to default."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(0, int(raw.strip()))
    except ValueError:
        print(f"Warning: {name}={raw!r} is not a number; using {default}.")
        return default


def _env_bool(name, default):
    """Reads a boolean from the environment, falling back to default."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


dotenv.load_dotenv('.env')

# --- Configuration ---
BUTTON_CONFIG_FILE = os.getenv("BUTTON_CONFIG_FILE", "buttons.json")
CONFIGURED_BUTTONS = []

# Encrypt any plaintext sensitive value found in buttons.json right after it is
# loaded, so an existing file is migrated without waiting for the next save.
ENCRYPT_SENSITIVE_ON_STARTUP = _env_bool("ENCRYPT_SENSITIVE_ON_STARTUP", True)

# --- Clipboard mode configuration ---
# Seconds before a copied value is taken off the clipboard again. 0 keeps the value
# until another copy replaces it.
CLIPBOARD_CLEAR_SECONDS = _env_int("CLIPBOARD_CLEAR_SECONDS", 15)
# Also clear the clipboard shortly after a Ctrl+V / Shift+Insert is observed.
CLIPBOARD_CLEAR_ON_PASTE = _env_bool("CLIPBOARD_CLEAR_ON_PASTE", True)
# Tag copies so Windows keeps them out of the Win+V history and the cloud clipboard.
CLIPBOARD_EXCLUDE_FROM_HISTORY = _env_bool("CLIPBOARD_EXCLUDE_FROM_HISTORY", True)
# Delay between the observed paste keystroke and the clear, so the target window has
# time to read the clipboard first.
PASTE_CLEAR_DELAY_MS = 300
# Per-row "copy to clipboard" button shown on every menu entry.
COPY_ICON = "\u29c9"
COPY_ICON_FONT = ('Segoe UI Symbol', 11)
COPY_TOOLTIP = "Copy to clipboard instead of typing"

# --- Global variable for the main hidden Tkinter root window ---
app_root = None

# --- Global variable to hold the Toplevel menu instance ---
menu_toplevel = None

# --- Global variable for the tooltip Toplevel instance ---
tooltip_toplevel = None

def resolve_dynamic_value(value):
    """
    Resolves special dynamic placeholders in the button value.

    A trailing newline (the "Press Enter" flag) is ignored while matching, so
    such a value still resolves and keeps its newline.
    """
    base, had_newline = split_trailing_newline(value)
    if base == "{{CURRENT_DATE}}":
        return datetime.now().strftime("%Y-%m-%d") + ("\n" if had_newline else "")
    return value


def format_log_value(raw_value, final_text):
    """
    Returns the text to log for a button action, masking sensitive values.
    """
    if any(
        entry.get("sensitive") and entry.get("value") == raw_value
        for entry in CONFIGURED_BUTTONS
    ):
        return "********"
    return final_text[:20]


# --- Clipboard helpers ---
def set_clipboard_text(text):
    """
    Puts text on the clipboard.

    The Win32 clipboard is preferred so a copy can be tagged as private, which keeps
    it out of the Windows clipboard history. Other platforms fall back to the Tk
    clipboard, which has no history to exclude.
    """
    if clipboard_win.is_supported():
        if clipboard_win.set_text(text, exclude_from_history=CLIPBOARD_EXCLUDE_FROM_HISTORY):
            return True
        print("Warning: could not write to the clipboard.")
        return False

    app_root.clipboard_clear()
    app_root.clipboard_append(text)
    app_root.update()
    return True


def get_clipboard_text():
    """Returns the clipboard text, or None when it does not hold plain text."""
    if clipboard_win.is_supported():
        return clipboard_win.get_text()

    try:
        return app_root.clipboard_get()
    except tk.TclError:
        return None


def clear_clipboard():
    """Empties the clipboard. Returns True when it was emptied."""
    if clipboard_win.is_supported():
        return clipboard_win.clear()

    app_root.clipboard_clear()
    app_root.update()
    return True


def type_text(text_to_type):
    """
    Types the given text into the currently active window and destroys the menu.
    """
    global menu_toplevel
    if menu_toplevel and menu_toplevel.winfo_exists():
        menu_toplevel.destroy()

    # Ensure tooltip is destroyed if it exists when a button is clicked
    hide_tooltip()

    time.sleep(0.05)

    final_text = resolve_dynamic_value(text_to_type)
    pyautogui.write(final_text)
    print(f"Typed: {format_log_value(text_to_type, final_text)}...")


# --- Clipboard mode ---
# The value QuickFill last put on the clipboard. The clipboard is only cleared while
# it still holds this exact text, so a copy made in another application is never
# thrown away by mistake.
_clipboard_expected = None
_clipboard_clear_job = None
_clipboard_armed = False


def copy_text(text_to_copy):
    """
    Copies the given text to the clipboard instead of typing it, and destroys the menu.

    Used by buttons flagged with "clipboard": true, for windows that ignore synthetic
    keystrokes but still accept a paste.
    """
    global menu_toplevel

    if menu_toplevel and menu_toplevel.winfo_exists():
        menu_toplevel.destroy()

    hide_tooltip()

    final_text = resolve_dynamic_value(text_to_copy)
    # A trailing newline means "press Enter" when typing. It is meaningless in a paste
    # and could submit a form, so it is dropped here. The stored value keeps it, so
    # switching the button back to typing restores the old behaviour.
    copy_value, _ = split_trailing_newline(final_text)

    if set_clipboard_text(copy_value):
        print(f"Copied to clipboard: {format_log_value(text_to_copy, final_text)}...")
        _schedule_clipboard_clear(copy_value)


def _schedule_clipboard_clear(expected):
    """Arms the auto-clear timer for the value that was just copied."""
    global _clipboard_clear_job, _clipboard_expected, _clipboard_armed

    _cancel_clipboard_clear()
    _clipboard_expected = expected
    _clipboard_armed = True

    if CLIPBOARD_CLEAR_SECONDS > 0:
        _clipboard_clear_job = app_root.after(
            CLIPBOARD_CLEAR_SECONDS * 1000, _clear_clipboard_if_unchanged
        )


def _cancel_clipboard_clear():
    """Cancels a pending auto-clear without touching the clipboard."""
    global _clipboard_clear_job

    if _clipboard_clear_job is not None:
        try:
            app_root.after_cancel(_clipboard_clear_job)
        except tk.TclError:
            pass
        _clipboard_clear_job = None


def _clear_clipboard_if_unchanged():
    """
    Empties the clipboard, but only while it still holds the value QuickFill put there.

    If something else was copied in the meantime it is left untouched.
    """
    global _clipboard_expected, _clipboard_clear_job, _clipboard_armed

    _clipboard_clear_job = None
    _clipboard_armed = False

    expected = _clipboard_expected
    _clipboard_expected = None

    if expected is None:
        return

    if get_clipboard_text() != expected:
        print("Clipboard changed since the copy; leaving it as it is.")
        return

    if clear_clipboard():
        print("Clipboard cleared.")
    else:
        print("Warning: could not clear the clipboard.")


def _on_paste_key():
    """Called from the key listener thread when a paste keystroke is seen."""
    global _clipboard_armed

    if not _clipboard_armed:
        return

    # Disarm now so a held key cannot queue several clears.
    _clipboard_armed = False
    # Give the focused window a moment to read the clipboard before clearing it.
    app_root.after(PASTE_CLEAR_DELAY_MS, _clear_clipboard_if_unchanged)


# --- Tooltip Functions ---
def show_tooltip(event, text):
    """
    Creates and displays a tooltip for a given widget.
    """
    global tooltip_toplevel
    # If a tooltip is already open, destroy it first
    if tooltip_toplevel and tooltip_toplevel.winfo_exists():
        tooltip_toplevel.destroy()

    if not text: # Don't show empty tooltips
        return

    tooltip_toplevel = tk.Toplevel(event.widget)
    tooltip_toplevel.wm_overrideredirect(True) # Remove window decorations
    # The menu is topmost, so a plain tooltip is drawn *behind* it and only the sliver
    # sticking out past the menu edge is visible. Matching the flag puts both windows
    # in the same Z-order band, where the tooltip stays above its owner (the menu):
    # its master is a widget inside the menu, which makes it an owned window.
    tooltip_toplevel.attributes('-topmost', True)

    label = tk.Label(tooltip_toplevel,
                     text=text,
                     background="#FFFFCC", # Light yellow background
                     relief="solid",      # Solid border
                     borderwidth=1,
                     font=("Arial", 8))
    label.pack(padx=5, pady=2)

    # Measure the finished tooltip first: it is positioned from its real size.
    tooltip_toplevel.update_idletasks()
    width = tooltip_toplevel.winfo_reqwidth()
    height = tooltip_toplevel.winfo_reqheight()

    # Anchor the tooltip just *outside* the menu rather than at the cursor. It is
    # topmost like the menu, so at the cursor it would sit on top of the rows below
    # and swallow their clicks - and, being above the pointer, send <Leave> to the
    # hovered button, which would destroy it again.
    menu_window = event.widget.winfo_toplevel()
    menu_x = menu_window.winfo_rootx()
    menu_width = menu_window.winfo_width()
    screen_width = tooltip_toplevel.winfo_screenwidth()
    screen_height = tooltip_toplevel.winfo_screenheight()

    # Prefer the right of the menu; flip to its left when the tooltip would not fit.
    x = menu_x + menu_width + 4
    if x + width > screen_width:
        x = max(0, menu_x - width - 4)
    # Line the tooltip up with the hovered row, clamped to the screen.
    y = min(max(0, event.widget.winfo_rooty()), max(0, screen_height - height))

    tooltip_toplevel.wm_geometry("+%d+%d" % (x, y))
    tooltip_toplevel.lift()

def hide_tooltip(event=None): # event is optional as it might be called without an event
    """
    Destroys the active tooltip.
    """
    global tooltip_toplevel
    if tooltip_toplevel and tooltip_toplevel.winfo_exists():
        tooltip_toplevel.destroy()
    tooltip_toplevel = None # Reset the global variable


# --- Shutdown ---
def quit_app():
    """
    Ends the application.

    Wired to the menu's Quit button and to the quit hotkey. Closing the menu is not
    enough on its own: the withdrawn root keeps ``mainloop()`` alive and both pynput
    listeners are daemon threads, so the process has to be ended here.
    """
    global menu_toplevel

    print("Quit requested. Shutting down...")
    if menu_toplevel and menu_toplevel.winfo_exists():
        menu_toplevel.destroy()
    hide_tooltip()

    # Unwind mainloop() for the normal case (menu only), then end the process
    # outright: an open button manager sits in a nested wait_window() loop that
    # survives quit(). Nothing is lost by exiting now - buttons.json is written only
    # on an explicit Save, and every save is atomic.
    if app_root:
        app_root.quit()
    sys.stdout.flush()
    os._exit(0)


def get_editor():
    """
    Determines which text editor to use based on environment variables or OS defaults.
    Priority: EDITOR_PATH > VISUAL > EDITOR > OS default.
    """
    # 1. Check for a specific path in the .env file
    editor = os.getenv("EDITOR_PATH")
    if editor:
        print(f"Using editor from EDITOR_PATH: {editor}")
        return editor

    # 2. Check standard environment variables (for Linux/macOS)
    if sys.platform != "win32":
        for key in ['VISUAL', 'EDITOR']:
            editor = os.environ.get(key)
            if editor:
                print(f"Using editor from ${key}: {editor}")
                return editor

    # 3. Fallback to OS-specific defaults
    if sys.platform == "win32":
        print("Falling back to default editor: notepad.exe")
        return "notepad.exe"
    else: # Linux, macOS, etc.
        print("Falling back to default editor: nano")
        return "nano" # A safe bet for most non-windows systems

def open_config_and_reload():
    """
    Opens the button configuration file in an editor and reloads it on close.
    """
    global menu_toplevel, CONFIGURED_BUTTONS

    if menu_toplevel and menu_toplevel.winfo_exists():
        menu_toplevel.destroy()
    hide_tooltip()

    editor = get_editor()
    config_file_path = os.path.abspath(BUTTON_CONFIG_FILE)
    print(f"Opening '{config_file_path}' with '{editor}'...")

    try:
        # Using shell=True is more robust for launching editors, especially on Windows.
        # It correctly handles paths with spaces (if quoted in the env var),
        # arguments (e.g., "code --wait"), and launching modern packaged apps.
        subprocess.run(f'{editor} "{config_file_path}"', check=True, shell=True)
        print("Editor closed. Reloading configuration...")
        CONFIGURED_BUTTONS = load_button_config(BUTTON_CONFIG_FILE)
        print(f"Configuration reloaded with {len(CONFIGURED_BUTTONS)} buttons.")
        # A hand-typed secret is a save too: encrypt it now that the editor closed.
        prepare_loaded_buttons(CONFIGURED_BUTTONS)

        # Keep an open manager window in sync with the hand-edited file.
        manager = get_button_manager()
        if manager is not None:
            manager.reload_from_disk()
    except FileNotFoundError:
        print(f"Error: Editor '{editor}' not found. Please check your system's PATH or set EDITOR_PATH in .env")
    except (subprocess.CalledProcessError, Exception) as e:
        print(f"An error occurred during the edit/reload process: {e}")


def reload_button_config():
    """Re-reads the button configuration file into the running application."""
    global CONFIGURED_BUTTONS
    CONFIGURED_BUTTONS = load_button_config(BUTTON_CONFIG_FILE)
    print(f"Configuration reloaded with {len(CONFIGURED_BUTTONS)} buttons.")
    prepare_loaded_buttons(CONFIGURED_BUTTONS)


def prepare_loaded_buttons(buttons):
    """
    Reports undecryptable values and encrypts plaintext sensitive ones.

    Called after the configuration is loaded, both at startup and after a
    reload. Values are encrypted on disk only; the in-memory list keeps its
    plaintext so typing and copying are unaffected. Problems are printed rather
    than raised, because a failure here must not stop the application from
    running with the values it already has.
    """
    failed = count_decrypt_failures(buttons)
    if failed:
        print(
            f"Warning: {failed} sensitive value(s) could not be decrypted. "
            "Open the manager, select each one and press Edit to enter it again."
        )

    if not ENCRYPT_SENSITIVE_ON_STARTUP:
        return

    try:
        migrate_plaintext_sensitive_values(BUTTON_CONFIG_FILE, buttons)
    except (OSError, crypto_store.CryptoError) as e:
        print(f"Warning: could not encrypt sensitive values in the configuration: {e}")


def open_manage_and_reload():
    """
    Opens the GUI button manager and reloads the configuration whenever the
    manager saves its changes, so new buttons appear without a restart.
    """
    global menu_toplevel

    if menu_toplevel and menu_toplevel.winfo_exists():
        menu_toplevel.destroy()
    hide_tooltip()

    try:
        buttons = read_button_config(BUTTON_CONFIG_FILE)
    except ConfigError as e:
        print(f"Error: {e}")
        messagebox.showerror(
            "QuickFill",
            f"{e}\n\nUse the \"Raw File\u2026\" button to fix the file by hand."
        )
        return

    open_button_manager(app_root, BUTTON_CONFIG_FILE, buttons, on_saved=reload_button_config)

def show_popup_menu():
    """
    Creates and displays the popup menu as a Toplevel window.
    This function *must* be called from the main Tkinter thread.
    """
    global menu_toplevel
    global app_root
    global CONFIGURED_BUTTONS

    if not CONFIGURED_BUTTONS:
        print("No buttons configured. Please check your button_config.json file.")
        return

    if menu_toplevel and menu_toplevel.winfo_exists():
        menu_toplevel.destroy()
    # Also ensure any lingering tooltip is gone when a new menu opens
    hide_tooltip()

    menu_toplevel = tk.Toplevel(app_root)
    menu_toplevel.title("QuickFill")
    menu_toplevel.attributes('-topmost', True)
    menu_toplevel.overrideredirect(True)

    x, y = pyautogui.position()
    # Position the menu slightly offset from the cursor so it doesn't immediately
    # trigger FocusOut if the cursor is directly over it.
    menu_toplevel.geometry(f"+{x+5}+{y+5}")

    for button_config in CONFIGURED_BUTTONS:
        button_text = button_config.get("text", "Unnamed Button")
        button_value = button_config.get("value", "")
        button_tooltip = button_config.get("tooltip", "") # Get the tooltip text

        # Each entry is a row: clicking the label types the value, clicking the copy
        # icon beside it puts the value on the clipboard instead. Some windows refuse
        # synthetic keystrokes but still accept a paste, so both are always available.
        row = tk.Frame(menu_toplevel)
        row.pack(fill='x', pady=2)

        button = tk.Button(
            row,
            text=button_text,
            command=lambda val=button_value: type_text(val),
            font=('Arial', 10),
            padx=10, pady=5
        )
        button.pack(side='left', fill='x', expand=True)

        copy_button = tk.Button(
            row,
            text=COPY_ICON,
            command=lambda val=button_value: copy_text(val),
            font=COPY_ICON_FONT,
            padx=6, pady=5
        )
        copy_button.pack(side='right', fill='y')

        # Bind events for the tooltip
        if button_tooltip: # Only bind if a tooltip text exists
            button.bind("<Enter>", lambda event, tt=button_tooltip: show_tooltip(event, tt))
            button.bind("<Leave>", hide_tooltip)

        copy_button.bind("<Enter>", lambda event: show_tooltip(event, COPY_TOOLTIP))
        copy_button.bind("<Leave>", hide_tooltip)

    # --- Action Buttons Frame ---
    action_frame = tk.Frame(menu_toplevel)
    action_frame.pack(fill='x', pady=2, padx=2)

    # --- Manage Button (GUI editor) ---
    manage_button = tk.Button(
        action_frame,
        text="Manage\u2026",
        command=open_manage_and_reload,
        font=('Arial', 8),
        padx=5, pady=2
    )
    manage_button.pack(side='left', fill='x', expand=True, padx=(0, 1))
    manage_button.bind("<Enter>", lambda event, tt="Add, edit or delete buttons in a window": show_tooltip(event, tt))
    manage_button.bind("<Leave>", hide_tooltip)

    # --- Raw File Button ---
    raw_file_button = tk.Button(
        action_frame,
        text="Raw File\u2026",
        command=open_config_and_reload,
        font=('Arial', 8),
        padx=5, pady=2
    )
    raw_file_button.pack(side='left', fill='x', expand=True, padx=(0, 1))
    raw_file_button.bind("<Enter>", lambda event, tt="Edit buttons.json in a text editor": show_tooltip(event, tt))
    raw_file_button.bind("<Leave>", hide_tooltip)

    # --- Quit Button ---
    # The first widget packed with side='right' lands furthest right, so Quit is
    # packed before Close to sit at the very edge, away from the harmless Close.
    quit_button = tk.Button(
        action_frame,
        text="Quit",
        command=quit_app,
        font=('Arial', 8),
        padx=5, pady=2
    )
    quit_button.pack(side='right', fill='x', expand=True, padx=(1, 0))
    quit_button.bind("<Enter>", lambda event, tt="Exit QuickFill completely (Ctrl+Shift+Q)": show_tooltip(event, tt))
    quit_button.bind("<Leave>", hide_tooltip)

    # --- Close Button ---
    close_button = tk.Button(
        action_frame,
        text="Close",
        command=lambda: menu_toplevel.destroy(),
        font=('Arial', 8),
        padx=5, pady=2
    )
    close_button.pack(side='right', fill='x', expand=True, padx=(1, 0))
    close_button.bind("<Enter>", lambda event, tt="Click to close the menu": show_tooltip(event, tt))
    close_button.bind("<Leave>", hide_tooltip)

    # --- Escape Key Binding ---
    menu_toplevel.bind("<Escape>", lambda event: menu_toplevel.destroy())

    def on_focus_out(event):
        # Destroy the menu when it loses focus
        if menu_toplevel and menu_toplevel.winfo_exists():
            menu_toplevel.destroy()
            # Also destroy any active tooltip when the menu closes
            hide_tooltip()

    menu_toplevel.bind("<FocusOut>", on_focus_out)
    menu_toplevel.focus_force()


def on_activate_hotkey():
    """
    This function is called when the hotkey is pressed.
    It schedules the menu creation to run on the main Tkinter thread.
    """
    print("Hot key pressed! Scheduling menu display...")
    if app_root:
        app_root.after(0, show_popup_menu)
    else:
        print("Error: Tkinter app_root not initialized.")


def on_quit_hotkey():
    """
    Handler for the quit hotkey. It runs on the pynput listener thread, so the
    shutdown itself is scheduled onto the Tkinter thread like any other UI action.
    """
    print("Quit hot key pressed!")
    if app_root:
        app_root.after(0, quit_app)


# --- Setup the Hotkey Listener ---
def start_hotkey_listener():
    hotkey = '<ctrl>+<shift>+z'
    listener = keyboard.GlobalHotKeys({
        hotkey: on_activate_hotkey,
        '<ctrl>+<shift>+q': on_quit_hotkey,
    })
    listener.daemon = True
    listener.start()
    print(f"Listening for hotkey: {hotkey} (quit: Ctrl+Shift+Q)")


def start_clipboard_paste_listener():
    """
    Watches for a paste keystroke so a copied value can be cleared straight after it is
    used, instead of waiting for the timer to expire.

    The listener is passive: the keystroke still reaches the focused window.
    """
    if not CLIPBOARD_CLEAR_ON_PASTE:
        return

    listener = keyboard.GlobalHotKeys({
        '<ctrl>+v': _on_paste_key,
        '<shift>+<insert>': _on_paste_key,
        '<ctrl>+<insert>': _on_paste_key,
    })
    listener.daemon = True
    listener.start()
    print("Listening for paste keys to clear the clipboard afterwards.")


if __name__ == "__main__":
    print("Starting QuickFill application...")

    # First run: leave a small example file behind so there is something to type
    # instead of exiting on an empty configuration.
    ensure_button_config_exists(BUTTON_CONFIG_FILE)

    CONFIGURED_BUTTONS = load_button_config(BUTTON_CONFIG_FILE)
    if not CONFIGURED_BUTTONS:
        print(
            "Application cannot start without button configurations. Exiting. "
            f"Add button objects to {BUTTON_CONFIG_FILE}, for example with the "
            "manager in a previous session or by copying the examples from the README."
        )
        exit()

    prepare_loaded_buttons(CONFIGURED_BUTTONS)

    app_root = tk.Tk()
    app_root.withdraw()
    app_root.attributes('-topmost', True)

    start_hotkey_listener()
    start_clipboard_paste_listener()

    if CLIPBOARD_CLEAR_SECONDS > 0:
        print(f"Clipboard buttons: value cleared after {CLIPBOARD_CLEAR_SECONDS}s.")
    else:
        print("Clipboard buttons: auto-clear timer disabled.")
    if CLIPBOARD_CLEAR_ON_PASTE:
        print("Clipboard buttons: also cleared right after a paste.")
    print(
        "Clipboard history exclusion: "
        f"{'on' if CLIPBOARD_EXCLUDE_FROM_HISTORY else 'off'}."
    )
    print(
        "Sensitive values in buttons.json: "
        f"{'encrypted' if crypto_store.is_supported() else 'NOT encrypted'}."
    )

    print("Tkinter main loop started. Press Ctrl+Shift+Q to quit, or Ctrl+C in console.")
    app_root.mainloop()

    print("Application exiting.")