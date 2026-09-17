"""One-time setup: securely save a cruise-line portal login to your OS's
secure credential store (Windows Credential Manager / macOS Keychain /
Linux Secret Service — the `keyring` library picks whichever is native)
so the automation doesn't need the password typed in every time.

RUN THIS YOURSELF, directly in your own terminal (or double-click
SAVE_LOGIN.bat on Windows / SAVE_LOGIN.command on macOS) — never paste a
password into a chat with Claude or anyone else. Uses getpass() so it's
never echoed to the screen, never written to any file, and never printed
anywhere by this script.

Storage is encrypted by your OS itself and tied to your own user account
on THIS device — there is no exportable file this produces: it cannot be
copied to a server, a USB drive, or another machine and read there.

Pasting your password in is fine — some terminals wrap pasted text in
invisible "bracketed paste" markers; this script strips those out (along
with surrounding whitespace) before saving, so a pasted password is saved
the same as a typed one, not corrupted by stray escape codes.

ESPRESSO (Royal Caribbean / Celebrity) requires MFA at login — saving a
password here does NOT make its login fully unattended the way MSC's is.
It only means the username/password fields can be pre-filled; you still
complete the MFA step yourself every time. (Actually auto-filling those
fields on the real login page is separate, not-yet-built work — this
script only stores the credential for whenever that's wired up.)

Usage:
    python save_login.py
"""

from __future__ import annotations

import getpass
import re

import keyring

from config.settings import settings

CRUISE_LINES = {
    "1": ("MSC", settings.msc_credential_service),
    "2": ("ESPRESSO (Royal Caribbean / Celebrity)", settings.espresso_credential_service),
    # NCL runs a SEPARATE SeaWeb agent account per market — confirmed by
    # Neon 2026-08-27 after 25 Canadian bookings all returned
    # "Reservation is not found" against the US login. Each market needs
    # its own credentials stored under its own service name.
    #
    # The US entry keeps the ORIGINAL, unsuffixed service name so anything
    # already saved keeps working with no migration; the suffix rule here
    # must stay in step with NclScraper.credential_service.
    "3": ("NCL — US account (USD)", settings.ncl_credential_service),
    "4": ("NCL — Canada account (CAD)", f"{settings.ncl_credential_service}_ca"),
}

# Strips bracketed-paste escape sequences some terminals wrap pasted text
# in (ESC[200~ ... ESC[201~) — getpass() doesn't strip these itself, and
# an unstripped marker would silently corrupt a pasted password.
_BRACKETED_PASTE_RE = re.compile(r"\x1b\[20[01]~")


def _clean(value: str) -> str:
    return _BRACKETED_PASTE_RE.sub("", value).strip()


def _prompt_password_console(label: str) -> str | None:
    """Masked password entry that ACTUALLY accepts a paste in a Windows
    console, with a live character counter as feedback.

    ADDED 2026-08-26 after a real, confirmed failure: a password entered
    through `getpass.getpass()` saved as only ONE character, and every
    NCL login failed afterward with no visible cause. The project owner
    then confirmed directly: "the password is not pasting."

    Why getpass fails here and this doesn't: `getpass` reads the terminal
    with echo disabled and returns on the first newline, and in several
    Windows terminal hosts a Ctrl+V paste is not delivered as ordinary
    keystrokes to that raw read — so the pasted text is dropped or only
    partially registers, while the blank prompt gives no indication
    anything went wrong. Reading via `msvcrt.getwch()` instead pulls
    characters directly off the console input buffer, which IS where a
    console paste lands (all characters arrive in sequence), so a paste
    of any length comes through intact.

    Echoes one '*' per character plus a running count, so a paste that
    silently fails is immediately obvious instead of invisible. Returns
    None when this isn't a real Windows console (e.g. Git Bash/MinTTY,
    SSH, a piped stdin) so the caller can fall back. Never prints,
    logs, or stores the value itself.
    """
    try:
        import msvcrt
    except Exception:
        return None  # not Windows — caller falls back

    # `sys.stdin.isatty()` is NOT a sufficient check here — confirmed
    # 2026-08-26: in a Git Bash / piped-stdin environment it returned
    # True while stdin was actually a pipe, and `msvcrt.getwch()` then
    # blocked forever waiting for console input that could never arrive
    # (it hung a real command until it was killed). GetConsoleMode
    # succeeds ONLY on a genuine console handle and fails on a pipe, so
    # it's the reliable discriminator — verified both ways on this
    # machine.
    try:
        import ctypes

        handle = ctypes.windll.kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if not ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return None  # stdin is a pipe/redirect, not a real console
    except Exception:
        return None

    print(f"\n{label} password (paste with Ctrl+V or right-click, then press Enter):")
    print("  ", end="", flush=True)
    chars: list[str] = []
    while True:
        try:
            ch = msvcrt.getwch()
        except Exception:
            # Anything unexpected here (non-console handle, etc.) —
            # discard the partial value and let the caller fall back
            # rather than saving something half-read.
            print()
            return None
        if ch in ("\r", "\n"):
            print()
            break
        if ch == "\x03":  # Ctrl+C
            print()
            raise KeyboardInterrupt
        if ch == "\x08":  # Backspace
            if chars:
                chars.pop()
                # Erase the '*' and rewrite the counter.
                print(f"\r  {'*' * len(chars)}  ({len(chars)} chars)   ", end="", flush=True)
            continue
        if ch == "\x00" or ch == "\xe0":
            # Function/arrow key prefix — consume the second byte and ignore.
            try:
                msvcrt.getwch()
            except Exception:
                pass
            continue
        chars.append(ch)
        print(f"\r  {'*' * len(chars)}  ({len(chars)} chars)   ", end="", flush=True)
    return "".join(chars)


def _prompt_password_gui(label: str) -> str | None:
    """Ask for the password in a small GUI window instead of the terminal.

    ADDED 2026-08-26 after a REAL, CONFIRMED failure: a password saved
    through `getpass.getpass()` came back only ONE character long, and
    every NCL login failed afterward. getpass reads the terminal in raw
    mode and, in several Windows terminal hosts, a Ctrl+V paste is NOT
    delivered as ordinary keystrokes — so the pasted text is silently
    dropped (or only partially registers) while the prompt shows nothing
    either way, giving zero feedback that anything went wrong.

    This matters because the project owner's ORIGINAL requirement for
    this tool was explicit: "PLEASE KEEP IN MIND THAT THESE PASSWORDS
    WILL BE COPY PASTED." A Tk entry widget handles Ctrl+V natively
    (and right-click paste), masks the characters, and can show a live
    character count as real feedback that the paste actually landed.

    tkinter is Python stdlib — no new dependency. Returns None if a GUI
    genuinely isn't available (SSH/headless), so the caller falls back
    to getpass. The value is never printed, logged, or written anywhere
    except the OS credential store by the caller.
    """
    try:
        import tkinter as tk
    except Exception:
        return None

    result: dict[str, str | None] = {"value": None}
    try:
        root = tk.Tk()
    except Exception:
        # No display available (headless/SSH) — caller falls back.
        return None

    root.title("CruiseIntel — Save Login")
    root.geometry("460x210")
    root.attributes("-topmost", True)

    tk.Label(root, text=f"{label} password", font=("Segoe UI", 11, "bold")).pack(pady=(16, 4))
    tk.Label(
        root,
        text="Paste with Ctrl+V (or right-click). Nothing is shown as you type,\n"
             "but the counter below confirms the paste landed.",
        font=("Segoe UI", 8),
        justify="center",
    ).pack()

    entry = tk.Entry(root, show="•", width=44, font=("Segoe UI", 11))
    entry.pack(pady=10)
    entry.focus_force()

    count_label = tk.Label(root, text="0 characters", font=("Segoe UI", 9), fg="#888")
    count_label.pack()

    def _update_count(*_):
        n = len(entry.get())
        count_label.config(
            text=f"{n} characters",
            fg="#888" if n >= 4 else "#c00",
        )

    entry.bind("<KeyRelease>", _update_count)
    # <<Paste>> fires on Ctrl+V/right-click paste; the count updates a
    # beat later so the widget has the pasted text by then.
    entry.bind("<<Paste>>", lambda e: root.after(50, _update_count))

    def _submit(*_):
        result["value"] = entry.get()
        root.destroy()

    def _cancel(*_):
        result["value"] = None
        root.destroy()

    entry.bind("<Return>", _submit)
    row = tk.Frame(root)
    row.pack(pady=8)
    tk.Button(row, text="Save", width=12, command=_submit).pack(side="left", padx=6)
    tk.Button(row, text="Cancel", width=12, command=_cancel).pack(side="left", padx=6)

    root.protocol("WM_DELETE_WINDOW", _cancel)
    root.mainloop()
    return result["value"]


def main() -> None:
    print("=" * 50)
    print("   CRUISEINTEL — SAVE LOGIN")
    print("=" * 50)
    print("\nNothing you type here is shown on screen, logged, or saved to a file.")
    print("It's stored encrypted, on THIS device only, by your operating system.\n")
    print("Which cruise line?")
    for key, (label, _) in CRUISE_LINES.items():
        print(f"  {key}. {label}")

    choice = input("\nType a number and press Enter: ").strip()
    if choice not in CRUISE_LINES:
        print("Not a valid choice — nothing was saved.")
        return

    label, service_name = CRUISE_LINES[choice]
    print(f"\nSaving login for {label}.")
    if service_name == settings.espresso_credential_service:
        print(
            "Note: ESPRESSO requires MFA at login — this saves your username/password so "
            "they can be filled in for you, but you'll still complete the MFA step yourself "
            "every time. It is not a fully hands-off login the way MSC's is.\n"
        )
    if service_name.startswith(settings.ncl_credential_service):
        if service_name != settings.ncl_credential_service:
            print(
                f"\nSaving the {label} credential under a SEPARATE keyring "
                f"entry ({service_name}) — it does NOT overwrite the US one. "
                f"Run this script again and pick the other NCL option to "
                f"store both accounts."
            )
        print(
            "Note: NCL's login flow isn't wired up to auto-fill anything yet — this only "
            "stores the credential for later. Also unconfirmed as of this writing: NCL may "
            "now route agent login through a newer SSO layer (\"Norwegian Central\") rather "
            "than a direct username/password form on seawebagents.ncl.com — check which one "
            "your login actually uses before assuming this saved credential applies as-is.\n"
        )

    username = _clean(input(f"{label} username / agent ID: "))

    # Fallback chain, in order of reliability for a PASTED password —
    # getpass is LAST because it's the one confirmed to silently drop a
    # paste on this machine (2026-08-26). Each returns None when it
    # isn't usable in the current environment.
    #   1. msvcrt console reader — works in cmd.exe/PowerShell, echoes
    #      '*' + a live count so a failed paste is visible immediately.
    #   2. Tk GUI dialog — for Git Bash/MinTTY or anywhere (1) can't run.
    #   3. getpass — last resort, and if it returns something
    #      implausibly short the length guard below catches it.
    raw_password = _prompt_password_console(label)
    if raw_password is None:
        raw_password = _prompt_password_gui(label)
    if raw_password is None:
        print("(Falling back to a plain hidden prompt — if your paste doesn't")
        print(" register here, run this from cmd.exe or PowerShell instead.)")
        raw_password = getpass.getpass(f"{label} password: ")
    if raw_password is None:
        print("Cancelled — nothing was saved.")
        return
    password = _clean(raw_password)

    if not username or not password:
        print("Username and password are both required — nothing was saved.")
        return

    # CONFIRMED REAL INCIDENT, 2026-08-26: an NCL password saved through
    # this script came back only ONE character long, which silently
    # failed every login afterward with no obvious cause (NCL shows no
    # error text on a rejected login, so it looked like a timeout/bug in
    # the scraper rather than a bad credential). Most likely a paste that
    # didn't fully register in the terminal, or Enter pressed early —
    # getpass() shows nothing as you type, so there is no visual feedback
    # to catch it. Confirm anything implausibly short rather than storing
    # it silently. Never prints or echoes the value itself.
    if len(password) < 4:
        print(
            f"\n⚠  The password you entered is only {len(password)} character(s) long."
        )
        print(
            "   getpass shows nothing as you type, so a paste that didn't fully "
            "register looks identical to a real entry.\n"
            "   A real incident on 2026-08-26 stored a 1-character password this way "
            "and every login failed afterward with no clear reason."
        )
        confirm = input("   Save it anyway? Type 'yes' to confirm: ").strip().lower()
        if confirm != "yes":
            # IMPORTANT (flaw found 2026-08-26, same day as the guard was
            # added): simply returning here leaves any PREVIOUSLY-saved
            # bad password in place — which is exactly what happened
            # after the 1-character NCL save. The user re-ran this
            # script, declined the guard, and reasonably assumed the bad
            # value was gone; it wasn't, so logins kept failing for the
            # same reason. Clear the stored credential so the state is
            # unambiguous: either a good password is saved, or nothing is.
            existing = keyring.get_password(service_name, "password")
            if existing is not None:
                for key in ("username", "password"):
                    try:
                        keyring.delete_password(service_name, key)
                    except Exception:
                        pass
                print(
                    f"   Nothing new was saved, AND the previously-stored {label} "
                    "login was cleared\n   (it would otherwise have kept failing "
                    "silently). Run this again to set it properly."
                )
            else:
                print("Nothing was saved. Run this again and re-enter the password.")
            return

    keyring.set_password(service_name, "username", username)
    keyring.set_password(service_name, "password", password)

    print(f"\nSaved ({len(username)}-char username, {len(password)}-char password).")
    print(f"Stored under your OS's secure credential store as '{service_name}'.")
    print("Windows: Control Panel > Credential Manager > Windows Credentials.")
    print("macOS: open the Keychain Access app and search for the service name above.")
    print("To remove it later, run clear_login.py.")


if __name__ == "__main__":
    main()
