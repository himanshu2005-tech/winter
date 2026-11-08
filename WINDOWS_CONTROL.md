# Windows control reliability

The supplied log showed Bluetooth control attempting to disable every healthy
PnP Bluetooth entry and then interpreting device counts and service status as
radio state. Those observations do not establish whether the radio is on or off.
PowerShell can also emit a non-terminating error and subsequently exit zero
after a successful listing. RUN_COMMAND now defaults ErrorActionPreference to
Stop, retaining its existing timeouts, cancellation and process-tree cleanup.
Native executable failures still require exit-code and state verification.

WINDOWS_RADIO is a declared, searchable, schema-checked capability with a real
dispatcher handler. It reads Wi-Fi/Bluetooth radio state or requests on/off via
Windows.Devices.Radios.Radio. It requests OS permission, checks the access
result, waits for state readback, and rejects mismatches. It never disables all
PnP devices or changes radios at startup. Unknown inputs and broad all-radio
mutations are rejected. Personal user authorization remains enforced; this
tool cannot run proactively or for an unauthenticated guest.

Examples:

```
<ACTION type="WINDOWS_RADIO" kind="all" state="status" />
<ACTION type="WINDOWS_RADIO" kind="bluetooth" state="off" />
<ACTION type="WINDOWS_RADIO" kind="wifi" state="on" />
```

Wi-Fi off disables the radio, which is stronger than disconnecting one network.
It intentionally removes the network path. Success is delivered from the local
readback and closes the turn without another cloud request. Remote Telegram
delivery can be lost once the network is off. Turning a radio on is not proof
that any saved network/headset has connected. A cloud-based assistant still
needs network access for new model-directed commands; Windows Quick Settings
can restore Wi-Fi when offline. No new offline language model was added.

Read-only hardware validation found both Wi-Fi and Bluetooth exposed by the
Windows radio API with state On. No radio was toggled during development.
Mutation, denied-access, stale-readback and invalid-argument paths use mocks.
Windows hardware switches, privacy controls, policies and UAC can constrain
what the current user process can do; this feature does not bypass them.

General Windows tasks retain the existing PowerShell, application-launch,
window, accessibility and browser controls. The model selects native controls
or inspects shell/help/docs, then verifies the requested state. This change
does not assert that every Windows application or protected operation is
automatable.

## General-purpose command execution

RUN_COMMAND now accepts cwd and shell, so execution can target an existing
project folder and use Windows PowerShell, installed PowerShell 7, or CMD.
Every call remains a fresh process. This supports arbitrary commands available
to the current Windows account, without adding a separate action per program.
The shell selector validates its enum, and invalid working directories fail
before a process starts. The managed process retains cancellation, timeouts
and process-tree cleanup; its console is hidden.

PowerShell starts with terminating-error behavior and propagates the last
native exit code, including when a later PowerShell listing succeeds. A native
failure deliberately handled by the script may need an explicit exit decision;
this does not rewrite arbitrary scripts or infer their success criteria.

Long output is summarized using its beginning and end. A private execution
log under vault/logs/commands records stdout/stderr, exit code, shell and cwd;
the model can use READ_FILE to inspect it. Streams are capped at two million
characters each and the log declares truncation. Logs are excluded from graph
indexing and are not automatically converted to personal facts. Successful
exit codes remain distinct from verification of the user's requested effect.

Tests use isolated folders to verify relative directories, paths with spaces,
PowerShell Unicode output, CMD execution, a real write/read file task, native
exit codes, long-log recovery, schema wiring, authorization and existing abort
behavior. Administrator elevation and UAC were not exercised. Windows access
controls remain authoritative; this is not an unattended UAC bypass or an
unbounded background-job service.

References:
- https://learn.microsoft.com/en-us/uwp/api/windows.devices.radios.radio
- https://learn.microsoft.com/en-us/uwp/api/windows.devices.radios.radio.requestaccessasync
- https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/netsh-wlan
