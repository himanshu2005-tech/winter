"""Windows radio controls via the OS radio API, not PnP device removal.

Provider documentation: Windows.Devices.Radios.Radio on Microsoft Learn.
No radios are changed at import or while constructing a command.
"""
import json


def command(kind='all', state='status'):
    kind = str(kind).strip().lower()
    state = str(state).strip().lower()
    if kind not in {'all', 'wifi', 'bluetooth'} or state not in {'status', 'on', 'off'}:
        raise ValueError('kind must be wifi, bluetooth or all; state must be status, on or off')
    if kind == 'all' and state != 'status':
        raise ValueError('Choose wifi or bluetooth before changing a radio')
    # Only validated enum literals enter this fixed script. Device names and
    # user-provided PowerShell are never interpolated.
    return r'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
    $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1
} | Select-Object -First 1
function AwaitRadio($operation, [Type]$resultType) {
    $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
    if (-not $task.Wait(15000)) { throw 'Windows radio API timed out' }
    return $task.Result
}
$kind = '__KIND__'
$requested = '__STATE__'
$radios = @(AwaitRadio ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]]))
$matches = @($radios | Where-Object { $kind -eq 'all' -or $_.Kind.ToString().ToLowerInvariant() -eq $kind })
if ($matches.Count -eq 0) { throw 'No matching radio is exposed by Windows on this device' }
$rows = @()
if ($requested -ne 'status') {
    $access = AwaitRadio ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) ([Windows.Devices.Radios.RadioAccessStatus])
    if ($access.ToString() -ne 'Allowed') { throw "Windows radio permission: $access. Check Windows privacy/device policy or hardware switch." }
}
foreach ($radio in $matches) {
    $before = $radio.State.ToString()
    $accessResult = 'NotRequested'
    if ($requested -ne 'status') {
        $desired = [Windows.Devices.Radios.RadioState]::$requested
        if ($radio.State -ne $desired) {
            $result = AwaitRadio ($radio.SetStateAsync($desired)) ([Windows.Devices.Radios.RadioAccessStatus])
            $accessResult = $result.ToString()
            if ($accessResult -ne 'Allowed') { throw "Windows refused radio change: $accessResult" }
        }
        $deadline = [DateTime]::UtcNow.AddSeconds(5)
        while ($radio.State -ne $desired -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 100 }
        if ($radio.State -ne $desired) { throw "Radio change unverified; observed $($radio.State), requested $requested" }
    }
    $rows += @{name=$radio.Name; kind=$radio.Kind.ToString(); before=$before; state=$radio.State.ToString(); access=$accessResult}
}
@{requested=$requested; radios=$rows; verified=$true} | ConvertTo-Json -Depth 5 -Compress
'''.replace('__KIND__', kind).replace('__STATE__', state)


def receipt(output, kind, state):
    if str(output).startswith(('[Error]', '[Interrupted]')):
        return str(output)
    try:
        payload = json.loads(output)
        rows = payload['radios']
        if payload.get('verified') is not True or not isinstance(rows, list) or not rows:
            raise ValueError('No radio readback')
        if kind != 'all' and any(str(row.get('kind', '')).lower() != kind for row in rows):
            raise ValueError('Readback returned a different radio kind')
        if state != 'status' and any(str(row.get('state', '')).lower() != state for row in rows):
            raise ValueError('Radio readback does not match request')
        summary = '; '.join(f"{row['kind']} ({row['name']}): {row['state']}" for row in rows)
        return '[Success] Windows radio state verified: ' + summary
    except (ValueError, KeyError, TypeError):
        return '[Error] Windows did not return a verified radio state. ' + str(output)[:700]
