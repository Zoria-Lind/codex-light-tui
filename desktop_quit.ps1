param(
    [Parameter(Mandatory)][int]$DesktopPid,
    [Parameter(Mandatory)][long]$ExpectedStart,
    [switch]$InspectOnly
)

$ErrorActionPreference = 'Stop'
$desktop = Get-Process -Id $DesktopPid -ErrorAction SilentlyContinue
if (!$desktop) { return 'already-closed' }
if ($desktop.ProcessName -ne 'ChatGPT' -or $desktop.StartTime.ToFileTimeUtc() -ne $ExpectedStart) {
    return 'mismatch'
}
if ($desktop.MainWindowHandle -eq [IntPtr]::Zero) { return 'window-unavailable' }

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$scope = [Windows.Automation.TreeScope]::Descendants
$fileMenuId = [Windows.Automation.PropertyCondition]::new(
    [Windows.Automation.AutomationElement]::AutomationIdProperty, 'application-menu-trigger-file-menu'
)
$fileMenu = $null
$activateAt = [DateTime]::UtcNow.AddSeconds(2)
$deadline = [DateTime]::UtcNow.AddSeconds(7)
$activated = $false
do {
    $desktop.Refresh()
    if ($desktop.HasExited) { return 'already-closed' }
    if ($desktop.MainWindowHandle -ne [IntPtr]::Zero) {
        try {
            $window = [Windows.Automation.AutomationElement]::FromHandle($desktop.MainWindowHandle)
            $fileMenu = $window.FindFirst($scope, $fileMenuId)
            if ($fileMenu -and !$fileMenu.Current.IsOffscreen) { break }
        } catch [Windows.Automation.ElementNotAvailableException] {
            $fileMenu = $null
        }
    }
    if (!$InspectOnly -and !$activated -and [DateTime]::UtcNow -ge $activateAt) {
        $activated = $true
        $null = (New-Object -ComObject WScript.Shell).AppActivate($DesktopPid)
    }
    $fileMenu = $null
    Start-Sleep -Milliseconds 300
} until ([DateTime]::UtcNow -ge $deadline)
if (!$fileMenu) { return 'menu-unavailable' }

$menu = $fileMenu.GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern)
$opened = $menu.Current.ExpandCollapseState -eq 'Collapsed'
$requested = $false
if ($opened) { $menu.Expand() }

try {
    $condition = [Windows.Automation.PropertyCondition]::new(
        [Windows.Automation.AutomationElement]::ControlTypeProperty, [Windows.Automation.ControlType]::MenuItem
    )
    $deadline = [DateTime]::UtcNow.AddSeconds(3)
    do {
        $items = @($window.FindAll($scope, $condition) | Where-Object {
            $_.Current.IsEnabled -and $_.Current.Name -match '(?i)\b(ChatGPT|Codex)\b.*\bCtrl\s*\+\s*Q\s*$'
        })
        if ($items.Count -eq 1) {
            $quit = $items[0].GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern)
            if ($InspectOnly) { return 'quit-item-available' }
            $quit.Invoke()
            $requested = $true
            break
        }
        Start-Sleep -Milliseconds 150
    } until ([DateTime]::UtcNow -ge $deadline)
} finally {
    if ($opened -and !$requested) { $menu.Collapse() }
}

if (!$requested) { return 'quit-item-unavailable' }
if ($desktop.WaitForExit(20000)) { return 'closed' }
return 'quit-requested-still-running'
