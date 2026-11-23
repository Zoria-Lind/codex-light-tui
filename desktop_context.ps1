param(
    [Parameter(Mandatory)][int]$DesktopPid,
    [Parameter(Mandatory)][long]$WindowHandle,
    [Parameter(Mandatory)][int]$RelayPort
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$desktop = Get-Process -Id $DesktopPid
if ($desktop.ProcessName -ne 'ChatGPT' -or $desktop.MainWindowHandle.ToInt64() -ne $WindowHandle) {
    throw 'Desktop window changed.'
}

# A verified socket from this desktop process tree is required. A local
# endpoint file alone cannot tell whether this window uses the shared server.
$clients = @(& netstat.exe -ano -p tcp | ForEach-Object {
    if ($_ -match "^\s*TCP\s+127\.0\.0\.1:\d+\s+127\.0\.0\.1:${RelayPort}\s+ESTABLISHED\s+(\d+)\s*$") {
        [int]$Matches[1]
    }
} | Select-Object -Unique)
$connected = $false
foreach ($client in $clients) {
    $ancestor = $client
    for ($depth = 0; $depth -lt 8 -and $ancestor -gt 0; $depth++) {
        if ($ancestor -eq $DesktopPid) { $connected = $true; break }
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ancestor"
        $ancestor = $process.ParentProcessId
    }
    if ($connected) { break }
}
if (!$connected) { throw 'Desktop is not connected to the shared App Server.' }

Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$window = [Windows.Automation.AutomationElement]::FromHandle([IntPtr]$WindowHandle)
$scope = [Windows.Automation.TreeScope]::Descendants
$modes = @($window.FindAll($scope, [Windows.Automation.PropertyCondition]::new(
    [Windows.Automation.AutomationElement]::ControlTypeProperty, [Windows.Automation.ControlType]::Button
)) | Where-Object {
    !$_.Current.IsOffscreen -and $_.Current.Name -match '^(Switch mode,\s*current mode|\u5207\u6362\u6A21\u5F0F\uFF0C\u5F53\u524D\u6A21\u5F0F)[:\uFF1A]\s*Codex$'
})
if ($modes.Count -ne 1) { throw 'Codex mode could not be verified.' }

$rootDocument = $window.FindFirst(
    $scope,
    [Windows.Automation.PropertyCondition]::new(
        [Windows.Automation.AutomationElement]::AutomationIdProperty, 'RootWebArea'
    )
)
if (!$rootDocument -or [string]::IsNullOrWhiteSpace($rootDocument.Current.Name)) {
    throw 'No chat title is available.'
}
[pscustomobject]@{
    title = $rootDocument.Current.Name
    created = $desktop.StartTime.ToFileTimeUtc()
} | ConvertTo-Json -Compress
