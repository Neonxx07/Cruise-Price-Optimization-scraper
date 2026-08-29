"""Removes the MSC Book login saved by msc_save_credentials.py from
Windows Credential Manager. Run this yourself if you ever want the saved
login gone.

Usage:
    C:\\cruisevenv\\venv\\Scripts\\python.exe msc_clear_credentials.py
"""

import keyring
from keyring.errors import PasswordDeleteError

from config.settings import settings

# Single source of truth is config/settings.py (consolidated 2026-08-11 —
# was hardcoded identically here and in msc_save_credentials.py/
# msc_commands.py before).
SERVICE_NAME = settings.msc_credential_service


def main():
    removed = False
    for key in ("username", "password"):
        try:
            keyring.delete_password(SERVICE_NAME, key)
            removed = True
        except PasswordDeleteError:
            pass
    print("Removed." if removed else "Nothing was saved.")


if __name__ == "__main__":
    main()
