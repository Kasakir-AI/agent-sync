# Approvals and backups

Before a run changes a file in your setup, it saves the original in
`~/.local/state/agent-sync/backups/run-<date-time>/`. Backups use paths relative to your resolved home folder.
Files outside your home go under `_outside/`. On macOS, upkeep uses APFS clones for folders when it can.
Global skill updates back up the skills with reported updates. When the Skills CLI updates only in bulk or
upkeep cannot read the update list, upkeep backs up every locked skill before updating.
A standalone update from `program_updates` backs up the resolved executable and its dedicated install folder under `~/.local` or `/usr/local/opt`, if present.
Upkeep prunes unchanged copies at the end of the run. A run with no changed originals leaves no backup folder.

Every `agent-sync run` checks retention, even when upkeep is skipped, offline, or fails. Backups expire after
30 days (`backup_days`). If the backup folder exceeds 500 MiB (`backup_max_mb`), upkeep deletes the oldest
agent-sync backup folders until it reaches the limit or exhausts eligible backups, starting with the large ones
(over a tenth of the limit), so one big program backup doesn't push out small ones. The current run and any
folders you add yourself can keep the total above the limit. Upkeep skips symlinked backup folders and entries
and asks you to acknowledge the warning. Run `agent-sync status` to see backup size, folder count, and both limits.

Cleanup also drops cached checkouts for packages you remove from `skill_packages`. It checks the checkout name
and Git origin before deleting a folder. Warning acknowledgements and Inbox notification records expire after
30 days. Log lines expire after `log_days`. On macOS, the scheduler's own output file (`launchd.out` in the state
folder, which holds only crash output) is cut to its newest 256 KiB once it passes 1 MiB. A dry run reports the
cleanup it would do.

A run never removes something you or another program installed until you approve it. This covers an MCP settings
entry that a plugin already brings, a Codex plugin replaced by Codex's own version, a skill a package dropped, an
edited skill or memory copy, a deletion from your source instructions, and replacing your own status line. A run
also never installs a new major version of a program it upgrades (`upgrade:<manager>:<package>@<major>`) until you
approve it. Keeping that key stays on your major version until an even newer one comes out; keeping
`upgrade:<manager>:<package>` stays on it for good.

- **Scheduled run (macOS):** a window lists what is waiting. Select what may go ahead and click **Approve selected**, or
  click **Keep all for now**. Anything you don't select is asked about again at the next run.
- **Terminal and the Desktop launcher:** the run asks yes or no for each item. The default is no.
- **Plannotator Inbox:** when Plannotator is installed, each run with something new posts to the Inbox thread
  "agent-sync upkeep" with a button to approve and a button to keep each item.
- **Any time:** `agent-sync pending` lists what is waiting. `agent-sync pending --approve <key>` lets the next run
  remove it. `agent-sync pending --keep <key>` adds it to `keep_installed`, which keeps it for good.

Approvals expire when upkeep confirms the item is gone. For MCP duplicates, upkeep checks each settings file
independently. A skipped step or an unreadable file preserves the approvals it could not check.

Each batch of deletions from the source instructions gets one `instructions:<digest>` key. Removing one of two
identical lines counts as one deletion. All instruction edits wait for that batch to be decided. Approve to apply
the deletion and share the other edits on the next run. Keep to restore the shared lines and resume sharing. Add
`"instructions"` to `keep_installed` to keep the lines in every deletion batch. Moving a shared line into a local,
private, or memory section that did not already contain it leaves the other tools' copies in place.

New warnings appear in a macOS alert that stays until you click **OK** (in a terminal, until you press Enter), and in
the Inbox. A warning you acknowledge stays quiet for 30 days unless its text changes. `agent-sync pending --ack`
acknowledges the recent warnings. `notify = false` turns off desktop windows and Inbox messages.
