# Installs two Windows scheduled tasks that run every 5 minutes, hidden, as you:
#   GtaSenderFeeder   -> clip_feeder.py  (clipper output -> queue, equal share per channel)
#   GtaSenderAutopost -> autopost.py     (queue -> Zernio -> YouTube + Instagram)
# Run once from the kit folder:  powershell -ExecutionPolicy Bypass -File scripts\install-tasks.ps1
# Remove them later with:        powershell -ExecutionPolicy Bypass -File scripts\install-tasks.ps1 -Remove
param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$kit = Split-Path -Parent $PSScriptRoot
$names = 'GtaSenderFeeder', 'GtaSenderAutopost'
if ($Remove) {
    foreach ($n in $names) { Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue }
    Write-Host 'Tasks removed.'; return
}
if (-not (Test-Path (Join-Path $kit 'config\channels.json'))) { throw 'config\channels.json is missing. Copy config\channels.example.json to config\channels.json and fill it in first.' }
if (-not (Test-Path (Join-Path $kit '.env'))) { throw '.env is missing. Copy .env.example to .env and put your Zernio key in it first.' }
$py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python.exe).Source }
python -m pip install -r (Join-Path $kit 'requirements.txt') | Out-Null
$map = @{ 'GtaSenderFeeder' = 'clip_feeder.py'; 'GtaSenderAutopost' = 'autopost.py' }
foreach ($n in $names) {
    $action  = New-ScheduledTaskAction -Execute $py -Argument (Join-Path $kit $map[$n]) -WorkingDirectory $kit
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
    $set     = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
    Register-ScheduledTask -TaskName $n -Action $action -Trigger $trigger -Settings $set -Force | Out-Null
    Write-Host "Installed $n (every 5 minutes)."
}
Write-Host 'Done. Check queue\autopost.log and queue\clip_feeder.log after about 6 minutes.'
