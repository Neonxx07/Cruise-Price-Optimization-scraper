"""Removes a cruise-line login saved by save_login.py from your OS's
secure credential store. Run this yourself if you ever want a saved
login gone (or double-click CLEAR_LOGIN.bat on Windows /
CLEAR_LOGIN.command on macOS).

Usage:
    python clear_login.py
"""

from __future__ import annotations

import keyring
from keyring.errors import PasswordDeleteError

from config.settings import settings

CRUISE_LINES = {
    "1": ("MSC", settings.msc_credential_service),
    "2": ("ESPRESSO (Royal Caribbean / Celebrity)", settings.espresso_credential_service),
    "3": ("NCL (Norwegian Cruise Line)", settings.ncl_credential_service),
}


def main() -> None:
    print("Which cruise line's saved login do you want to remove?")
    for key, (label, _) in CRUISE_LINES.items():
        print(f"  {key}. {label}")

    choice = input("\nType a number and press Enter: ").strip()
    if choice not in CRUISE_LINES:
        print("Not a valid choice — nothing was removed.")
        return

    label, service_name = CRUISE_LINES[choice]
    removed = False
    for key in ("username", "password"):
        try:
            keyring.delete_password(service_name, key)
            removed = True
        except PasswordDeleteError:
            pass
    print(f"Removed {label} login." if removed else f"Nothing was saved for {label}.")


if __name__ == "__main__":
    main()
