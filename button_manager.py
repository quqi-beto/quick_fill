"""
GUI button manager for the QuickFill popup tool.

This module owns the on-disk button configuration (``buttons.json``) and the
Tkinter windows used to add, edit and delete button objects without having to
hand-edit the JSON file.

The manager window is deliberately a normal, decorated Toplevel parented to the
hidden application root. It must not be a child of the popup menu, because the
popup is ``overrideredirect`` and destroys itself on ``<FocusOut>``.

This module is also the place where sensitive values are encrypted for storage:
``read_button_config`` decrypts them and ``save_button_config`` encrypts them, so
the rest of the application only ever works with plaintext values.
"""

import copy
import json
import os
import shutil
import tempfile
import tkinter as tk
from tkinter import messagebox, ttk

import crypto_store

# Canonical key order used for newly created button objects.
FIELD_ORDER = ("text", "value", "sensitive", "tooltip")
KNOWN_FIELDS = frozenset(FIELD_ORDER)

# Label shown when an entry has no usable "text" value.
UNNAMED_BUTTON = "Unnamed Button"

# Buttons written when the configuration file does not exist yet, so a
# first-time user sees the expected shape and a few of the available features.
# The values are plain text on purpose: they are templates to be edited, and a
# sensitive one is encrypted by the normal startup migration.
DEFAULT_BUTTONS = [
    {
        "text": "My Email",
        "value": "user@example.com",
        "tooltip": "Click the label to type this text, or the copy icon to paste it.",
    },
    {
        "text": "Today's Date",
        "value": "{{CURRENT_DATE}}",
        "tooltip": "{{CURRENT_DATE}} is typed as today's date (dynamic value).",
    },
    {
        "text": "Username + Enter",
        "value": "your.username\n",
        "tooltip": "A value ending in \\n presses Enter after typing.",
    },
    {
        "text": "My Password",
        "value": "your-password-here\n",
        "sensitive": True,
        "tooltip": "sensitive: true encrypts the value in buttons.json and hides it from the log.",
    },
]

# Suffix appended to sensitive entries in the list. Values are never shown.
SENSITIVE_MARKER = "   [sensitive]"

# Suffix appended when a sensitive value could not be decrypted and must be
# re-entered through Edit.
DECRYPT_FAILED_MARKER = "   [cannot decrypt]"

# Runtime-only key set on an entry whose sensitive value failed to decrypt.
# Keys starting with "_" are never written back to buttons.json.
DECRYPT_ERROR_FIELD = "_decrypt_error"
RUNTIME_FIELD_PREFIX = "_"


class ConfigError(Exception):
    """Raised when the button configuration file cannot be used."""


# ---------------------------------------------------------------------------
# Configuration file access
# ---------------------------------------------------------------------------

def read_button_config(file_path):
    """
    Loads button configurations from a JSON file.

    Raises ConfigError when the file is missing, is not valid JSON, or does not
    contain a list of button objects.
    """
    if not os.path.exists(file_path):
        raise ConfigError(f"Button configuration file not found at {file_path}")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"Invalid JSON in {file_path}: {e}") from e
    except OSError as e:
        raise ConfigError(f"Could not read {file_path}: {e}") from e

    if not isinstance(config, list):
        raise ConfigError(
            f"{file_path} should contain a list of button objects, "
            f"found {type(config).__name__}."
        )

    decrypt_sensitive_values(config)

    print(f"Loaded {len(config)} button configurations from {file_path}")
    return config


def load_button_config(file_path):
    """
    Loads button configurations, tolerating a missing or malformed file.

    Problems are printed and an empty list is returned so that application
    startup stays tolerant of a broken configuration file.
    """
    try:
        return read_button_config(file_path)
    except ConfigError as e:
        print(f"Error: {e}")
        return []


def _write_button_config(file_path, stored):
    """
    Writes an already-prepared button list to file_path atomically.

    ``stored`` must be the exact data to serialize: sensitive values already
    encrypted and runtime-only keys removed. The previous contents are copied to
    "<file_path>.bak" first, so a bad save can be rolled back by hand. Returns
    the absolute path that was written.

    Raises OSError on failure, leaving the original file untouched.
    """
    file_path = os.path.abspath(file_path)
    directory = os.path.dirname(file_path) or "."

    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        shutil.copy2(file_path, file_path + ".bak")

    handle = None
    tmp_path = None
    try:
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=".buttons-",
            suffix=".tmp",
            delete=False,
        )
        tmp_path = handle.name
        json.dump(stored, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        # os.replace is atomic and replaces the destination on Windows too.
        os.replace(tmp_path, file_path)
        tmp_path = None
    finally:
        if handle is not None:
            handle.close()
        if tmp_path is not None and os.path.exists(tmp_path):
            os.remove(tmp_path)

    return file_path


def save_button_config(file_path, buttons):
    """
    Writes the button list to file_path atomically.

    Sensitive values are encrypted and runtime-only keys are dropped from the
    written copy; the caller's in-memory list is left untouched, so the manager
    keeps working with plaintext. The previous contents are copied to
    "<file_path>.bak" first, so a bad save can be rolled back by hand.

    Raises OSError or crypto_store.CryptoError on failure, leaving the original
    file untouched.
    """
    # Encrypt before touching the file: a CryptoError must not leave a partial
    # or plaintext copy behind.
    stored = encrypt_sensitive_values(buttons)
    file_path = _write_button_config(file_path, stored)

    print(f"Saved {len(stored)} button configurations to {file_path}")


def ensure_button_config_exists(file_path):
    """
    Creates file_path with DEFAULT_BUTTONS when it does not exist yet.

    Called once at startup so a first run has something to type instead of
    exiting immediately. Values are written in plain text, so a sensitive
    example is encrypted by the normal startup migration rather than here, and
    a machine without DPAPI still gets a usable file. Returns True when a new
    file was written; a failure is printed and reported as False so startup can
    continue and report the missing file itself.
    """
    if os.path.exists(file_path):
        return False

    try:
        written = _write_button_config(file_path, copy.deepcopy(DEFAULT_BUTTONS))
    except OSError as e:
        print(f"Warning: could not create {file_path}: {e}")
        return False

    print(
        f"Created a new button configuration at {written} with "
        f"{len(DEFAULT_BUTTONS)} example buttons."
    )
    return True


# ---------------------------------------------------------------------------
# Sensitive value encryption
# ---------------------------------------------------------------------------

def strip_runtime_fields(entries):
    """
    Returns a deep copy of the entries without runtime-only keys.

    A key starting with "_" only exists while the application is running (for
    example the note that a value could not be decrypted) and must never be
    written back to buttons.json.
    """
    cleaned = []
    for entry in entries:
        if not isinstance(entry, dict):
            cleaned.append(copy.deepcopy(entry))
            continue
        cleaned.append({
            key: copy.deepcopy(value)
            for key, value in entry.items()
            if not (isinstance(key, str) and key.startswith(RUNTIME_FIELD_PREFIX))
        })
    return cleaned


def encrypt_sensitive_values(entries):
    """
    Returns a copy of the entries with sensitive values encrypted for storage.

    Empty values, non-string values and values that are already encrypted are
    left as they are. Non-sensitive entries are never encrypted.
    """
    stored = strip_runtime_fields(entries)
    for entry in stored:
        if not isinstance(entry, dict) or not entry.get("sensitive"):
            continue
        value = entry.get("value")
        if isinstance(value, str) and value and not crypto_store.is_encrypted(value):
            entry["value"] = crypto_store.encrypt_text(value)
    return stored


def decrypt_sensitive_values(entries):
    """
    Decrypts sensitive values in place and returns the number of failures.

    A plaintext value in a sensitive entry is left as it is, so a hand-edited
    file still works and gets encrypted on the next save. A value that cannot be
    decrypted is replaced by an empty string and marked with the runtime-only
    "_decrypt_error" key, so the button never types its ciphertext and the user
    can simply re-enter the value through Edit.
    """
    failures = 0
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("sensitive"):
            continue
        value = entry.get("value")
        if not crypto_store.is_encrypted(value):
            continue
        try:
            entry["value"] = crypto_store.decrypt_text(value)
        except crypto_store.CryptoError as e:
            entry["value"] = ""
            entry[DECRYPT_ERROR_FIELD] = True
            failures += 1
            print(f'Warning: could not decrypt "{entry_display_text(entry)}": {e}')
    return failures


def count_decrypt_failures(entries):
    """Returns how many entries are marked as failing to decrypt."""
    return sum(
        1
        for entry in entries
        if isinstance(entry, dict) and entry.get(DECRYPT_ERROR_FIELD)
    )


def has_plaintext_secrets(entries):
    """True when a sensitive entry still holds a plaintext, non-empty value."""
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("sensitive"):
            continue
        value = entry.get("value")
        if isinstance(value, str) and value and not crypto_store.is_encrypted(value):
            return True
    return False


def stored_has_plaintext_secrets(file_path):
    """
    True when the on-disk configuration still holds a plaintext sensitive value.

    The in-memory list always holds plaintext, because that is the whole point of
    decrypting on load, so the decision to rewrite the file has to be made
    against what is actually stored. Returns False when the file is missing or
    cannot be parsed; the caller has already reported that separately.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            stored = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False

    if not isinstance(stored, list):
        return False
    return has_plaintext_secrets(stored)


def migrate_plaintext_sensitive_values(file_path, buttons):
    """
    Encrypts plaintext sensitive values that are still stored in file_path.

    ``buttons`` must be the freshly loaded, plaintext list. The file is rewritten
    only when the stored copy still contains a plaintext sensitive value, so a
    configuration that is already encrypted is never rewritten and its ".bak"
    rollback copy is left alone. Returns the number of encrypted entries and
    propagates OSError / CryptoError on failure.
    """
    if not buttons or not stored_has_plaintext_secrets(file_path):
        return 0

    count = sum(
        1
        for entry in buttons
        if isinstance(entry, dict)
        and entry.get("sensitive")
        and isinstance(entry.get("value"), str)
        and entry.get("value")
        and not crypto_store.is_encrypted(entry.get("value"))
    )

    save_button_config(file_path, buttons)
    print(
        f"Encrypted {count} plaintext sensitive value(s) in "
        f"{os.path.basename(file_path)}."
    )
    return count


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------

def split_trailing_newline(value):
    """
    Splits a single trailing newline off a stored value.

    Returns a (value_without_trailing_newline, had_trailing_newline) tuple.
    """
    if isinstance(value, str) and value.endswith("\n"):
        return value[:-1], True
    return value, False


def entry_display_text(entry):
    """Returns the label to show for an entry, falling back to a default."""
    text = entry.get("text")
    if isinstance(text, str) and text.strip():
        return text
    return UNNAMED_BUTTON


def build_entry(fields):
    """
    Creates a new button object in the canonical key order from the field
    values returned by ButtonEditDialog.
    """
    entry = {}
    apply_entry_fields(entry, fields)
    return entry


def apply_entry_fields(entry, fields):
    """
    Updates the known fields of an entry in place.

    Known fields are written in the canonical order and any unknown keys that a
    hand-edited file may contain are preserved. Runtime-only keys (the "_"
    prefix) are dropped, so saving an entry after entering a new value also
    clears a previous "cannot decrypt" note.
    """
    updated = {"text": fields["text"], "value": fields["value"]}
    if fields.get("sensitive"):
        updated["sensitive"] = True
    if fields.get("tooltip"):
        updated["tooltip"] = fields["tooltip"]

    for key, value in entry.items():
        if key in KNOWN_FIELDS:
            continue
        if isinstance(key, str) and key.startswith(RUNTIME_FIELD_PREFIX):
            continue
        updated[key] = value

    entry.clear()
    entry.update(updated)
    return entry


# ---------------------------------------------------------------------------
# Add / Edit form
# ---------------------------------------------------------------------------

class ButtonEditDialog(tk.Toplevel):
    """
    Modal form used to create a new button or edit an existing one.

    After wait_window() returns, ``result`` holds the field values, or None when
    the dialog was cancelled.
    """

    def __init__(self, parent, title, entry=None, all_entries=()):
        super().__init__(parent)

        self.result = None
        self._entry = entry
        self._all_entries = all_entries

        source = entry or {}
        value, trailing_newline = split_trailing_newline(source.get("value", ""))

        self._text_var = tk.StringVar(value=str(source.get("text") or ""))
        self._value_var = tk.StringVar(value=str(value))
        self._sensitive_var = tk.BooleanVar(value=bool(source.get("sensitive")))
        self._enter_var = tk.BooleanVar(value=trailing_newline)
        self._tooltip_var = tk.StringVar(value=str(source.get("tooltip") or ""))
        self._show_value_var = tk.BooleanVar(value=False)

        self.title(title)
        self.resizable(False, False)
        self.attributes("-topmost", True)

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(body, text="Text (button label):").grid(row=row, column=0, sticky="w", pady=(0, 6))
        self._text_entry = ttk.Entry(body, textvariable=self._text_var, width=44)
        self._text_entry.grid(row=row, column=1, columnspan=2, sticky="we", pady=(0, 6))
        row += 1

        ttk.Label(body, text="Value (text to type):").grid(row=row, column=0, sticky="w", pady=(0, 6))
        self._value_entry = ttk.Entry(body, textvariable=self._value_var, width=44)
        self._value_entry.grid(row=row, column=1, sticky="we", pady=(0, 6))
        self._show_check = ttk.Checkbutton(
            body, text="Show", variable=self._show_value_var, command=self._sync_value_visibility
        )
        self._show_check.grid(row=row, column=2, sticky="w", padx=(6, 0), pady=(0, 6))
        row += 1

        ttk.Checkbutton(
            body,
            text="Sensitive (encrypt in buttons.json, mask the console log)",
            variable=self._sensitive_var,
            command=self._sync_value_visibility,
        ).grid(row=row, column=1, columnspan=2, sticky="w", pady=(0, 4))
        row += 1

        ttk.Checkbutton(
            body,
            text="Press Enter after typing (stores a trailing \\n)",
            variable=self._enter_var,
        ).grid(row=row, column=1, columnspan=2, sticky="w", pady=(0, 4))
        row += 1

        ttk.Label(body, text="Tooltip (optional):").grid(row=row, column=0, sticky="w", pady=(0, 6))
        ttk.Entry(body, textvariable=self._tooltip_var, width=44).grid(
            row=row, column=1, columnspan=2, sticky="we", pady=(0, 6)
        )
        row += 1

        actions = ttk.Frame(body)
        actions.grid(row=row, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(actions, text="Cancel", command=self._on_cancel).pack(side="right")
        ttk.Button(actions, text="OK", command=self._on_ok).pack(side="right", padx=(0, 6))

        self._sync_value_visibility()

        self.bind("<Escape>", lambda event: self._on_cancel())
        self.bind("<Return>", lambda event: self._on_ok())
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        self.update_idletasks()
        self._center_on(parent)
        self.transient(parent)
        self.grab_set()

        self._text_entry.focus_set()
        if entry is not None:
            self._text_entry.selection_range(0, "end")

    def _center_on(self, parent):
        """Positions the dialog near the centre of its parent window."""
        try:
            parent.update_idletasks()
            x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
            y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
        except tk.TclError:
            return
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _sync_value_visibility(self):
        """Masks the value while the entry is marked sensitive and hidden."""
        sensitive = self._sensitive_var.get()
        if not sensitive:
            self._show_value_var.set(False)

        masked = sensitive and not self._show_value_var.get()
        self._value_entry.configure(show="*" if masked else "")
        self._show_check.configure(state="normal" if sensitive else "disabled")

    def _on_ok(self):
        text = self._text_var.get().strip()
        if not text:
            messagebox.showwarning(
                "QuickFill", "Text is required: it is the button label.", parent=self
            )
            self._text_entry.focus_set()
            return

        duplicate = any(
            other is not self._entry and entry_display_text(other) == text
            for other in self._all_entries
        )
        if duplicate and not messagebox.askyesno(
            "QuickFill",
            f'A button named "{text}" already exists.\n\nKeep this one anyway?',
            parent=self,
        ):
            return

        value = self._value_var.get()
        if self._enter_var.get():
            value += "\n"

        self.result = {
            "text": text,
            "value": value,
            "sensitive": self._sensitive_var.get(),
            "tooltip": self._tooltip_var.get().strip(),
        }
        self.destroy()

    def _on_cancel(self):
        self.result = None
        self.destroy()


# ---------------------------------------------------------------------------
# Manager window
# ---------------------------------------------------------------------------

class ButtonManagerWindow(tk.Toplevel):
    """
    Lists every configured button and allows adding, editing and deleting them.

    Edits are held in a working copy until Save is pressed, so an accidental
    change can always be discarded with Close.
    """

    def __init__(self, master, config_path, buttons, on_saved=None):
        super().__init__(master)

        self._config_path = os.path.abspath(config_path)
        self._on_saved = on_saved
        self.buttons = copy.deepcopy(list(buttons))
        self.dirty = False

        self.title("QuickFill - Manage Buttons")
        self.minsize(440, 320)
        self.geometry("480x400")
        self.attributes("-topmost", True)

        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)
        body.rowconfigure(2, weight=1)
        body.columnconfigure(0, weight=1)

        ttk.Label(body, text=f"File: {self._config_path}", anchor="w").grid(
            row=0, column=0, sticky="we"
        )
        ttk.Label(
            body,
            text="Changes are kept in memory until you press Save.",
            anchor="w",
            foreground="#555555",
        ).grid(row=1, column=0, sticky="we", pady=(0, 6))

        list_frame = ttk.Frame(body)
        list_frame.grid(row=2, column=0, sticky="nsew")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        self._tree = ttk.Treeview(
            list_frame, columns=("button",), show="headings", selectmode="browse"
        )
        self._tree.heading("button", text="Button")
        self._tree.column("button", anchor="w", width=400, stretch=True)
        self._tree.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self._tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._tree.configure(yscrollcommand=scrollbar.set)
        self._tree.bind("<Double-1>", lambda event: self._on_edit())

        actions = ttk.Frame(body)
        actions.grid(row=3, column=0, sticky="we", pady=(8, 0))
        ttk.Button(actions, text="Add\u2026", command=self._on_add).pack(side="left")
        ttk.Button(actions, text="Edit\u2026", command=self._on_edit).pack(side="left", padx=(4, 0))
        ttk.Button(actions, text="Delete", command=self._on_delete).pack(side="left", padx=(4, 0))
        ttk.Button(actions, text="Close", command=self._on_close).pack(side="right")
        ttk.Button(actions, text="Save", command=self._on_save).pack(side="right", padx=(0, 4))

        self._status_var = tk.StringVar(value=self._loaded_status())
        ttk.Label(
            body, textvariable=self._status_var, anchor="w", foreground="#444444"
        ).grid(row=4, column=0, sticky="we", pady=(6, 0))

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Destroy>", self._on_destroy)

        self._refresh_list(select_index=0 if self.buttons else None)
        self.after(50, self._grab_focus)

    # -- window plumbing ---------------------------------------------------

    def _grab_focus(self):
        if not self.winfo_exists():
            return
        self.lift()
        self.focus_force()
        self._tree.focus_set()

    def _on_destroy(self, event):
        if event.widget is self:
            _clear_manager_reference(self)

    def _update_title(self):
        self.title("QuickFill - Manage Buttons" + (" *" if self.dirty else ""))

    def _set_status(self, message):
        self._status_var.set(message)

    def _loaded_status(self):
        """Builds the initial status line, warning about undecryptable values."""
        failed = count_decrypt_failures(self.buttons)
        if failed:
            return (
                f"{len(self.buttons)} button(s) loaded; {failed} value(s) could not "
                "be decrypted. Select one, press Edit and enter the value again."
            )
        return f"{len(self.buttons)} button(s) loaded."

    # -- list helpers ------------------------------------------------------

    def _refresh_list(self, select_index=None):
        self._tree.delete(*self._tree.get_children())

        for index, entry in enumerate(self.buttons):
            label = entry_display_text(entry)
            if entry.get("sensitive"):
                label += SENSITIVE_MARKER
            if entry.get(DECRYPT_ERROR_FIELD):
                label += DECRYPT_FAILED_MARKER
            self._tree.insert("", "end", iid=str(index), values=(label,))

        if select_index is not None and 0 <= select_index < len(self.buttons):
            iid = str(select_index)
            self._tree.selection_set(iid)
            self._tree.focus(iid)
            self._tree.see(iid)

        self._update_title()

    def _selected_index(self):
        selection = self._tree.selection()
        if not selection:
            return None
        try:
            return int(selection[0])
        except (TypeError, ValueError):
            return None

    # -- actions -----------------------------------------------------------

    def _on_add(self):
        dialog = ButtonEditDialog(self, "Add Button", entry=None, all_entries=self.buttons)
        self.wait_window(dialog)
        if dialog.result is None:
            return

        self.buttons.append(build_entry(dialog.result))
        self.dirty = True
        self._refresh_list(select_index=len(self.buttons) - 1)
        self._set_status(
            f'Added "{dialog.result["text"]}". Press Save to write buttons.json.'
        )

    def _on_edit(self):
        index = self._selected_index()
        if index is None:
            self._set_status("Select a button first, then press Edit.")
            return

        entry = self.buttons[index]
        dialog = ButtonEditDialog(self, "Edit Button", entry=entry, all_entries=self.buttons)
        self.wait_window(dialog)
        if dialog.result is None:
            return

        apply_entry_fields(entry, dialog.result)
        self.dirty = True
        self._refresh_list(select_index=index)
        self._set_status(
            f'Updated "{dialog.result["text"]}". Press Save to write buttons.json.'
        )

    def _on_delete(self):
        index = self._selected_index()
        if index is None:
            self._set_status("Select a button first, then press Delete.")
            return

        label = entry_display_text(self.buttons[index])
        if not messagebox.askyesno(
            "QuickFill",
            f'Delete the button "{label}"?\n\nThe change is applied when you press Save.',
            parent=self,
        ):
            return

        del self.buttons[index]
        self.dirty = True
        self._refresh_list(select_index=min(index, len(self.buttons) - 1))
        self._set_status(f'Deleted "{label}". Press Save to write buttons.json.')

    def _on_save(self):
        """Writes the working copy to disk. Returns True when the save succeeded."""
        if not self.buttons and not messagebox.askyesno(
            "QuickFill",
            "The button list is empty. QuickFill exits at startup when no buttons "
            "are configured.\n\nSave an empty list anyway?",
            parent=self,
        ):
            return False

        try:
            save_button_config(self._config_path, self.buttons)
        except (OSError, crypto_store.CryptoError) as e:
            messagebox.showerror(
                "QuickFill",
                f"Could not save the configuration:\n{e}\n\n"
                "The file was left unchanged. If buttons.json is open in another "
                "program, close it and try again.",
                parent=self,
            )
            self._set_status("Save failed.")
            return False

        self.dirty = False
        self._update_title()
        self._set_status(
            f"Saved {len(self.buttons)} button(s). Previous version kept in "
            f"{os.path.basename(self._config_path)}.bak"
        )

        if self._on_saved is not None:
            self._on_saved()
        return True

    def _on_close(self):
        if self.dirty:
            answer = messagebox.askyesnocancel(
                "QuickFill",
                "You have unsaved changes.\n\nSave them before closing?",
                parent=self,
            )
            if answer is None:
                return
            if answer and not self._on_save():
                return
        self.destroy()

    def reload_from_disk(self):
        """
        Re-reads the configuration file, discarding the in-memory working copy.

        Used after the file was edited externally through the raw-file editor.
        """
        if self.dirty and not messagebox.askyesno(
            "QuickFill",
            "buttons.json was changed outside this window.\n\n"
            "Reload it and discard your unsaved changes?",
            parent=self,
        ):
            self._set_status("Not reloaded: buttons.json changed on disk.")
            return

        try:
            buttons = read_button_config(self._config_path)
        except ConfigError as e:
            self._set_status(f"Could not reload: {e}")
            return

        self.buttons = copy.deepcopy(buttons)
        self.dirty = False
        self._refresh_list(select_index=0 if self.buttons else None)
        failed = count_decrypt_failures(self.buttons)
        if failed:
            self._set_status(
                f"Reloaded {len(self.buttons)} button(s) from disk; {failed} value(s) "
                "could not be decrypted. Press Edit to enter them again."
            )
        else:
            self._set_status(f"Reloaded {len(self.buttons)} button(s) from disk.")


# ---------------------------------------------------------------------------
# Single-instance access helpers
# ---------------------------------------------------------------------------

_manager_window = None


def get_button_manager():
    """Returns the live manager window, or None when it is not open."""
    if _manager_window is not None and _manager_window.winfo_exists():
        return _manager_window
    return None


def _clear_manager_reference(window):
    global _manager_window
    if _manager_window is window:
        _manager_window = None


def open_button_manager(master, config_path, buttons, on_saved=None):
    """
    Opens the button manager, or brings the existing one back to the front.

    Must be called from the Tkinter main thread. ``on_saved`` is invoked after
    every successful save so the caller can reload its configuration.
    """
    global _manager_window

    existing = get_button_manager()
    if existing is not None:
        existing.deiconify()
        existing.lift()
        existing.focus_force()
        return existing

    _manager_window = ButtonManagerWindow(master, config_path, buttons, on_saved=on_saved)
    return _manager_window
