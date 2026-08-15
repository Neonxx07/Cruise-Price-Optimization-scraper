"""One-time setup: securely save your MSC Book login to Windows Credential
Manager so the automation can log itself back in without you typing your
password every time the session expires.

RUN THIS YOURSELF, directly in your own terminal — do not paste your
password into a chat with Claude or anyone else. This script uses
getpass() so your password is never echoed to the screen, never written
to any file, and never printed anywhere. It's encrypted by Windows itself
(DPAPI, the same mechanism behind saved Wi-Fi passwords) and tied to your
Windows user account — unreadable by any other Windows user on this PC,
and unreadable if the file were copied to a different machine.

Usage:
    C:\\cruisevenv\\venv\\Scripts\\python.exe msc_save_credentials.py
"""

import getpass

import keyring

from config.settings import settings

# Single source of truth is config/settings.py (consolidated 2026-08-11 —
# was hardcoded identically here and in msc_clear_credentials.py/
# msc_commands.py before).
SERVICE_NAME = settings.msc_credential_service


def main():
    print("This saves your MSC Book login to Windows Credential Manager.")
    print("Nothing you type here is shown on screen, logged, or saved to a file.\n")

    username = input("MSC Book username / agent ID: ").strip()
    password = getpass.getpass("MSC Book password (hidden): ")

    if not username or not password:
        print("Username and password are both required — nothing was saved.")
        return

    keyring.set_password(SERVICE_NAME, "username", username)
    keyring.set_password(SERVICE_NAME, "password", password)

    print(f"\nSaved. Credentials are stored under Windows Credential Manager as '{SERVICE_NAME}'.")
    print("You can verify this yourself: Control Panel > Credential Manager > Windows Credentials.")
    print("To remove them later, run msc_clear_credentials.py.")


if __name__ == "__main__":
    main()
