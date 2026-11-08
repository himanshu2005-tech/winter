$ErrorActionPreference = 'Stop'

$tailscaleCommand = Get-Command tailscale.exe -ErrorAction SilentlyContinue
if (-not $tailscaleCommand) {
    $installedPath = 'C:\Program Files\Tailscale\tailscale.exe'
    if (Test-Path -LiteralPath $installedPath) {
        $tailscaleCommand = Get-Item -LiteralPath $installedPath
    }
}

if (-not $tailscaleCommand) {
    Write-Error 'Tailscale is not installed. Install it from https://tailscale.com/download/windows and sign in first.'
}

$tailscalePath = $tailscaleCommand.Source
if (-not $tailscalePath) { $tailscalePath = $tailscaleCommand.FullName }

$statusText = & $tailscalePath status --json 2>$null
if ($LASTEXITCODE -ne 0 -or -not $statusText) {
    Write-Error 'Tailscale is installed but not signed in. Open Tailscale, sign in, then run this script again.'
}

$status = $statusText | ConvertFrom-Json
if ($status.BackendState -eq 'NeedsLogin') {
    Write-Host 'Tailscale is installed but needs you to sign in.'
    Write-Host 'Starting the Tailscale sign-in flow now...'
    & $tailscalePath up
    if ($LASTEXITCODE -ne 0) {
        Write-Error 'Tailscale sign-in did not complete. Open the Tailscale app from the Start menu, sign in, then run this script again.'
    }

    $statusText = & $tailscalePath status --json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $statusText) {
        Write-Error 'Tailscale did not return a status after sign-in. Open the Tailscale app and confirm it says Connected.'
    }
    $status = $statusText | ConvertFrom-Json
}

if ($status.BackendState -ne 'Running') {
    Write-Error "Tailscale is not connected (state: $($status.BackendState)). Open the Tailscale app, connect it, then retry."
}

Write-Host 'Publishing the local VANTA gateway privately through Tailscale HTTPS...'
& $tailscalePath serve --bg --yes 8765
if ($LASTEXITCODE -ne 0) {
    Write-Error 'Tailscale Serve could not be configured. Follow the consent URL printed above, then run this script again.'
}

Write-Host ''
Write-Host 'VANTA mobile access is ready:'
& $tailscalePath serve status
Write-Host ''
Write-Host 'Use the https://...ts.net address shown above on the VANTA pairing screen.'
