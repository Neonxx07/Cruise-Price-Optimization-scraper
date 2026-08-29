"""One persistent, interactively-driven MSC browser session.

Logs in once (phase 1, unrecorded), then loops forever reading simple
one-line commands from a command file and writing the result to a result
file — letting the session be driven step by step from outside (Claude,
reacting to what Neon reports seeing) without ever closing and reopening
the browser, which is what triggers MSC's "logon ID used in another
location" rejection (same shape of problem as ESPRESSO's bot-detection —
see msc_project_knowledge.md).

The actual command implementations live in msc_commands.py and are
hot-reloaded (importlib.reload) before every single command — this means
new commands/capabilities can be added to msc_commands.py at any time
without ever restarting this controller (and therefore without ever
needing to log in again). See msc_commands.py's module docstring for the
current command list.

After each command, the result is written to data/msc_control/result.txt
and command.txt is deleted, signalling readiness for the next command.
"""

import asyncio
import importlib
import os
import sys

from playwright.async_api import async_playwright

import msc_commands

sys.stdout.reconfigure(line_buffering=True)

STORAGE_STATE_PATH = "browser-profile/storage_state_MSC.json"
CONTROL_DIR = "data/msc_control"
COMMAND_PATH = os.path.join(CONTROL_DIR, "command.txt")
RESULT_PATH = os.path.join(CONTROL_DIR, "result.txt")
START_SIGNAL = os.path.join(CONTROL_DIR, "start")


async def wait_for_file(path: str, poll_s: float = 1.0) -> None:
    while not os.path.exists(path):
        await asyncio.sleep(poll_s)


def should_delete_command_file(processed_command: str, current_file_content: str | None) -> bool:
    """Whether it's safe to delete command.txt after finishing
    `processed_command` — extracted as a pure function (2026-08-13, Phase 0
    correctness audit) so the race-condition fix in the main loop below is
    directly unit-testable without a live browser. Safe only when the file
    still contains EXACTLY what was just processed; a None (file already
    gone) or a different value (overwritten with a new command while we
    were busy) must never be deleted — see the main loop's own comment for
    the full incident this prevents."""
    return current_file_content is not None and current_file_content == processed_command


async def main():
    # SESSION-SAFETY GUARD, added 2026-08-27. Without this, a second
    # controller could start and: (1) open a second MSC session on an
    # account the portal only allows ONE active session for — the "logon
    # ID used in another location" kick this whole design exists to avoid;
    # (2) delete the first controller's in-flight command.txt/result.txt
    # (see the stale-file cleanup right below, which is exactly why the
    # lock must be taken BEFORE it runs); (3) both poll command.txt and
    # both execute the same command, driving one real booking from two
    # browsers at once; (4) clobber storage_state_MSC.json, letting a dead
    # session overwrite a live one.
    #
    # Acquired before ANY destructive setup, and released in the finally.
    from services.resource_governor import SingleInstanceGuard

    guard = SingleInstanceGuard("msc_driver")
    if not guard.acquire():
        print("=" * 68)
        print("  ANOTHER MSC DRIVER IS ALREADY RUNNING — refusing to start.")
        print(f"  Holder: {guard.holder_pid() or 'unknown'}")
        print()
        print("  Two MSC drivers fight over the same portal session and can")
        print("  corrupt each other's command/result files and saved session.")
        print("  Close the other controller (or the GUI's MSC scan) first.")
        print("=" * 68)
        return

    try:
        await _run_controller()
    finally:
        guard.release()


async def _run_controller():
    """The real controller loop. Split out from main() so the
    single-instance guard above wraps every path, including early
    returns and exceptions."""
    os.makedirs(CONTROL_DIR, exist_ok=True)
    for stale in (COMMAND_PATH, RESULT_PATH, START_SIGNAL):
        if os.path.exists(stale):
            os.remove(stale)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("about:blank")
        state = {"context": context, "page": page, "pages": [page]}

        print("PHASE 1 — attempting automatic login (saved credentials)...")
        login_result = await msc_commands.auto_login(page)
        if login_result == "OK":
            print("Auto-login succeeded — no manual step needed.")
        else:
            print(f"Auto-login did not complete cleanly ({login_result}).")
            print("Please log in to MSC manually in this window now.")
            print(f"Then create this file to continue: {START_SIGNAL}")
            await wait_for_file(START_SIGNAL)
        print("\nReady. Waiting for commands in", COMMAND_PATH)

        while True:
            await wait_for_file(COMMAND_PATH)
            with open(COMMAND_PATH, encoding="utf-8") as f:
                command = f.read().strip()
            print(f"\n>>> {command}")
            importlib.reload(msc_commands)
            try:
                result = await msc_commands.run_command(state, command)
            except Exception as e:
                result = f"ERROR: {e}"
            print(result[:500])
            with open(RESULT_PATH, "w", encoding="utf-8") as f:
                f.write(result)
            # CONFIRMED REAL RISK, fixed 2026-08-13 (Phase 0 correctness
            # audit): this used to unconditionally os.remove(COMMAND_PATH)
            # — deleting whatever is CURRENTLY on disk at that path, not
            # necessarily the same content read at the top of this loop. A
            # second command written while run_command() above was still
            # executing (which can take many seconds for a batch command)
            # would sit on disk and then be silently deleted here, never
            # executed, with no error surfaced anywhere but the sender's
            # own timeout. Only delete the file if it still contains
            # EXACTLY the command just processed — if it was overwritten
            # in the meantime, that new command must survive to be picked
            # up on the next loop iteration (wait_for_file returns
            # immediately since the file already exists), never silently
            # discarded. Does not change behavior for the normal case
            # (no overlapping command) at all.
            try:
                with open(COMMAND_PATH, encoding="utf-8") as f:
                    current_content = f.read().strip()
            except FileNotFoundError:
                current_content = None
            if should_delete_command_file(command, current_content):
                os.remove(COMMAND_PATH)
            else:
                print(
                    "NOTE: command.txt changed while the previous command was "
                    "still running — leaving the new command in place instead "
                    "of deleting it; it will be picked up on the next loop."
                )
            await context.storage_state(path=STORAGE_STATE_PATH)


# ADDED 2026-08-13 (Phase 0 correctness audit): guarded so this module can
# be imported (e.g. by a test importing should_delete_command_file) without
# immediately launching a real browser — standard Python idiom, already
# used elsewhere in this project (see main.py). Behavior when actually run
# as a script (`python msc_session_controller.py`) is unchanged.
if __name__ == "__main__":
    asyncio.run(main())
