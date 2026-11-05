# run_nightly.py を毎日 4:00 に動かすタスクを登録する。スリープ中なら起こして実行する。
#
# 実行方法（PowerShell で、このフォルダーにいる状態で）:
#   powershell -ExecutionPolicy Bypass -File .\register_nightly_task.ps1
#
# 時刻を変える:      -At "03:30"
# Python を指定する:  -PythonW "C:\path\to\pythonw.exe"
# 登録を消す:        Unregister-ScheduledTask -TaskName "OTP Off-Role Insights Nightly"

param(
    [string]$At = "04:00",
    [string]$PythonW = "",
    [string]$TaskName = "OTP Off-Role Insights Nightly"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $PythonW) {
    # Microsoft Store 版 Python の pythonw.exe（画面を出さずに動く）を優先する
    $PythonW = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WindowsApps" -Directory -Filter "PythonSoftwareFoundation.Python.*" -ErrorAction SilentlyContinue |
        Sort-Object { [version]($_.Name -replace '^PythonSoftwareFoundation\.Python\.([\d.]+)_.*$', '$1') } -Descending |
        ForEach-Object { Join-Path $_.FullName "pythonw.exe" } |
        Where-Object { Test-Path $_ } |
        Select-Object -First 1
    if (-not $PythonW) {
        $command = Get-Command pythonw -ErrorAction SilentlyContinue
        if ($command) { $PythonW = $command.Source }
    }
}
if (-not $PythonW -or -not (Test-Path $PythonW)) {
    Write-Error "pythonw.exe が見つかりません。-PythonW でパスを指定してください。"
}

if (-not (Test-Path (Join-Path $root "nightly.local.json"))) {
    Write-Warning "nightly.local.json がありません。nightly.example.json をコピーして作ってください。"
}

$action = New-ScheduledTaskAction -Execute $PythonW -Argument "`"$root\run_nightly.py`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $At
# WakeToRun: スリープ解除 / StartWhenAvailable: 4 時に PC が落ちていたら次に起動したとき実行
# IgnoreNew: 前回が動いていたら新しく始めない
$settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 20)
# スリープ中もログオンは続いているので「ログオン時のみ実行」でよい
# （Store 版 Python は「ログオンしているかどうかにかかわらず実行」だと起動できないことがある）
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "OTP Off-Role Insights の夜間収集（run_nightly.py）" `
    -Force | Out-Null

# 電源につないでいるときのスリープ解除タイマーを有効にする
powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1
powercfg /SETACTIVE SCHEME_CURRENT

Write-Output "登録しました: $TaskName（毎日 $At）"
Write-Output "Python: $PythonW"
Write-Output "スリープ解除タイマー（電源接続時）を有効にしました。"
