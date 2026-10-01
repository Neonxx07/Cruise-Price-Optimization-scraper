"""Wait for the GUI to write a FRESH ESPRESSO session, then run the matrix.

The session only reaches disk in BaseScraper.stop(), i.e. on GUI shutdown.
This waits for storage_state_ESPRESSO.json to be rewritten, confirms it
actually looks alive, and only then runs the authenticated matrix - so the
operator's only job is to close the GUI.
"""
import json
import pathlib
import subprocess
import sys
import time

STATE = pathlib.Path("browser-profile/storage_state_ESPRESSO.json")
BASELINE = STATE.stat().st_mtime
DEADLINE = time.time() + 15 * 60

print(f"  waiting for a fresh session (current file: "
      f"{time.strftime('%H:%M:%S', time.localtime(BASELINE))})")
print("  -> close the CruiseIntel GUI now; it writes the session on shutdown\n",
      flush=True)

while time.time() < DEADLINE:
    if STATE.stat().st_mtime > BASELINE:
        time.sleep(3)                      # let the write finish
        data = json.loads(STATE.read_text(encoding="utf-8"))
        cookies = data.get("cookies") or []
        now = time.time()
        live = [c for c in cookies
                if c.get("expires", -1) <= 0 or c["expires"] > now]
        print(f"  fresh session written at "
              f"{time.strftime('%H:%M:%S', time.localtime(STATE.stat().st_mtime))}"
              f"  cookies={len(cookies)} live={len(live)}\n", flush=True)
        break
    time.sleep(5)
else:
    print("  timed out - the GUI was not closed, nothing was run")
    sys.exit(2)

sys.exit(subprocess.call([sys.executable, "espresso_authenticated_matrix.py"]))
