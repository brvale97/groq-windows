<#
End-to-end check of a built folder build on a real, unlocked Windows desktop.

Runs the app in the isolated "qa" profile (own settings folder, secret name,
mutex; never autostarts) with offline QA transcripts instead of Groq, next to
an installed copy. It presses the shortcut with SendInput, records from the
real default microphone, clicks the status bubble and checks that both
transcripts are pasted into a separate target window that keeps the focus.

  powershell -ExecutionPolicy Bypass -File desktop-e2e.ps1 -App <folder>\GroqInsertDictation.exe -Out <folder>
Must run in the interactive session (for example via a scheduled task).
#>
param([Parameter(Mandatory)] [string]$App, [Parameter(Mandatory)] [string]$Out)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $Out | Out-Null
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class E2E {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] struct KEYBDINPUT { public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public IntPtr extra; }
    [StructLayout(LayoutKind.Sequential)] struct MOUSEINPUT { public int dx; public int dy; public uint data; public uint dwFlags; public uint time; public IntPtr extra; }
    [StructLayout(LayoutKind.Explicit)] struct UNION { [FieldOffset(0)] public KEYBDINPUT ki; [FieldOffset(0)] public MOUSEINPUT mi; }
    [StructLayout(LayoutKind.Sequential)] struct INPUT { public uint type; public UNION u; }
    [DllImport("user32.dll")] static extern uint SendInput(uint n, INPUT[] inputs, int size);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern IntPtr FindWindow(string cls, string title);
    public static IntPtr Find(string title) { return FindWindow(null, title); }
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr h, uint flags);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern IntPtr GetWindowLongPtr(IntPtr h, int index);
    [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint msg, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, System.Text.StringBuilder s, int n);
    public static uint ForegroundPid() { uint pid; GetWindowThreadProcessId(GetForegroundWindow(), out pid); return pid; }
    public static string ForegroundTitle() { var s = new System.Text.StringBuilder(256); GetWindowText(GetForegroundWindow(), s, 256); return s.ToString(); }
    public static long Foreground() { IntPtr f = GetForegroundWindow(); return f == IntPtr.Zero ? 0 : GetAncestor(f, 2).ToInt64(); }
    public static void Key(ushort vk, bool up) {
        var i = new INPUT[1]; i[0].type = 1; i[0].u.ki.wVk = vk; i[0].u.ki.dwFlags = up ? 2u : 0u;
        SendInput(1, i, Marshal.SizeOf(typeof(INPUT))); System.Threading.Thread.Sleep(25);
    }
    public static void Click(int x, int y) {
        SetCursorPos(x, y); System.Threading.Thread.Sleep(60);
        foreach (uint flag in new uint[] { 2u, 4u }) {
            var i = new INPUT[1]; i[0].type = 0; i[0].u.mi.dwFlags = flag;
            SendInput(1, i, Marshal.SizeOf(typeof(INPUT))); System.Threading.Thread.Sleep(40);
        }
    }
    public static void Hotkey() { // ctrl+alt+shift+F24
        Key(0x11, false); Key(0x12, false); Key(0x10, false); Key(0x87, false);
        Key(0x87, true); Key(0x10, true); Key(0x12, true); Key(0x11, true);
    }
}
'@
[E2E]::SetProcessDPIAware() | Out-Null
$result = [ordered]@{ started = (Get-Date -Format o); foreground = @(); errors = @() }
$targetFile = Join-Path $Out 'target.txt'
$targetScript = Join-Path $Out 'target.ps1'
@"
Add-Type -AssemblyName System.Windows.Forms
`$f = New-Object Windows.Forms.Form; `$f.Text = 'Groq E2E doelvenster'; `$f.Width = 700; `$f.Height = 240; `$f.StartPosition = 'Manual'; `$f.Left = 80; `$f.Top = 80
`$t = New-Object Windows.Forms.TextBox; `$t.Multiline = `$true; `$t.Dock = 'Fill'; `$t.Font = New-Object Drawing.Font('Segoe UI', 12); `$f.Controls.Add(`$t)
`$timer = New-Object Windows.Forms.Timer; `$timer.Interval = 150; `$timer.Add_Tick({ [IO.File]::WriteAllText('$targetFile', `$t.Text) }); `$timer.Start()
`$f.Add_Shown({ `$t.Focus() }); [Windows.Forms.Application]::Run(`$f)
"@ | Set-Content -LiteralPath $targetScript -Encoding UTF8
Set-Content -LiteralPath $targetFile -Value '' -NoNewline
$profileDir = Join-Path $env:APPDATA 'GroqInsertDictation-qa'
Remove-Item -LiteralPath $profileDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $profileDir | Out-Null
'{"shortcut": "ctrl+alt+shift+f24", "autostart": false, "input_device": "", "language": "nl"}' | Set-Content -LiteralPath "$profileDir\settings.json" -Encoding ASCII
$target = Start-Process powershell.exe -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',$targetScript -PassThru -WindowStyle Normal
$env:GROQ_DICTATION_PROFILE = 'qa'
$env:GROQ_DICTATION_QA_TRANSCRIPTS = 'Eerste dictaat via sneltoets.|Tweede dictaat, gestopt met de bubbel.'
$appProcess = $null
function Wait-Until([scriptblock]$Condition, [double]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) { if (& $Condition) { return $true }; Start-Sleep -Milliseconds 100 }
    return [bool](& $Condition)
}
function Log-Has([string]$Pattern) { (Test-Path "$profileDir\app.log") -and [bool](Select-String -Path "$profileDir\app.log" -Pattern $Pattern -SimpleMatch) }
function Log-Count([string]$Pattern) { if (Test-Path "$profileDir\app.log") { @(Select-String -Path "$profileDir\app.log" -Pattern $Pattern -SimpleMatch).Count } else { 0 } }
function Grab-Bubble([string]$Name) {
    $h = [E2E]::Find('Groq Insert Dictation - Opname'); if ($h -eq [IntPtr]::Zero) { $h = [E2E]::Find('Groq Insert Dictation - Transcriptie') }
    if ($h -eq [IntPtr]::Zero) { return }
    $r = New-Object E2E+RECT; [E2E]::GetWindowRect($h, [ref]$r) | Out-Null
    $pad = 40; $w = $r.Right - $r.Left + 2 * $pad; $hgt = $r.Bottom - $r.Top + 2 * $pad
    $bmp = New-Object Drawing.Bitmap($w, $hgt); $g = [Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($r.Left - $pad, $r.Top - $pad, 0, 0, $bmp.Size); $bmp.Save((Join-Path $Out "bubble-$Name.png")); $g.Dispose(); $bmp.Dispose()
}
function Fg-Step([string]$Step) {
    $procId = [E2E]::ForegroundPid()
    $name = (Get-Process -Id $procId -ErrorAction SilentlyContinue).ProcessName
    $isTarget = [E2E]::Foreground() -eq $script:targetHwnd.ToInt64()
    # Only names of our own windows are recorded; other titles may be private.
    $title = if ($isTarget -or $name -eq 'GroqInsertDictation') { [E2E]::ForegroundTitle() } else { '' }
    $script:result.foreground += @{ step = $Step; target = $isTarget; process = $name; title = $title }
}
function Close-FirstRunSettings {
    # Without an API key the app opens Settings once after startup (first-run behaviour); close it.
    $deadline = (Get-Date).AddSeconds(8)
    while ((Get-Date) -lt $deadline) {
        $h = [E2E]::Find('Groq Insert Dictation instellingen')
        if ($h -ne [IntPtr]::Zero) { [E2E]::PostMessage($h, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null; Start-Sleep -Milliseconds 600; return $true }
        Start-Sleep -Milliseconds 200
    }
    return $false
}
function Shot-Window([IntPtr]$Hwnd, [string]$File) {
    $r = New-Object E2E+RECT; [E2E]::GetWindowRect($Hwnd, [ref]$r) | Out-Null
    $bmp = New-Object Drawing.Bitmap(($r.Right - $r.Left), ($r.Bottom - $r.Top)); $g = [Drawing.Graphics]::FromImage($bmp); $dc = $g.GetHdc()
    [E2E]::PrintWindow($Hwnd, $dc, 2) | Out-Null; $g.ReleaseHdc($dc); $bmp.Save($File); $g.Dispose(); $bmp.Dispose()
}
function Focus-Target {
    [E2E]::Key(0x12, $false); [E2E]::Key(0x12, $true)
    [E2E]::SetForegroundWindow($script:targetHwnd) | Out-Null
    return (Wait-Until { [E2E]::Foreground() -eq $script:targetHwnd.ToInt64() } 3)
}
try {
    if (-not (Wait-Until { [E2E]::Find('Groq E2E doelvenster') -ne [IntPtr]::Zero } 15)) { throw 'Target window did not open' }
    $script:targetHwnd = [E2E]::GetAncestor([E2E]::Find('Groq E2E doelvenster'), 2)
    $appProcess = Start-Process -FilePath $App -WorkingDirectory (Split-Path $App) -PassThru
    if (-not (Wait-Until { Log-Has 'Startup ready' } 30)) { throw 'App did not finish startup' }
    $result.startup = (Select-String -Path "$profileDir\app.log" -Pattern 'Startup ready' | Select-Object -Last 1).Line
    $result.closed_first_run_settings = Close-FirstRunSettings
    if (-not (Focus-Target)) { throw 'Target window could not get the focus' }
    Fg-Step 'before shortcut'
    # 1. Shortcut starts and stops; the first transcript is pasted.
    [E2E]::Hotkey()
    if (-not (Wait-Until { Log-Has 'Opname gestart' } 6)) { throw 'Shortcut did not start a recording' }
    Start-Sleep -Milliseconds 900
    $bubble = [E2E]::Find('Groq Insert Dictation - Opname')
    $result.bubble_found = $bubble -ne [IntPtr]::Zero
    $result.bubble_noactivate = (([E2E]::GetWindowLongPtr($bubble, -20).ToInt64() -band 0x08000000) -ne 0)
    Fg-Step 'recording bubble shown'
    Grab-Bubble 'recording'
    Start-Sleep -Milliseconds 900
    [E2E]::Hotkey()
    if (-not (Wait-Until { (Get-Content -LiteralPath $targetFile -Raw) -like 'Eerste dictaat*' } 12)) { throw "First paste missing: $(Get-Content -LiteralPath $targetFile -Raw)" }
    Fg-Step 'after first paste'
    Wait-Until { (Log-Count 'Klaar. Gebruik je shortcut') -ge 1 } 5 | Out-Null
    # 2. Shortcut starts, a click on the bubble stops; focus stays in the target.
    [E2E]::Hotkey()
    if (-not (Wait-Until { (Log-Count 'Opname gestart') -ge 2 } 6)) { throw 'Second recording did not start' }
    Start-Sleep -Milliseconds 1800
    $bubble = [E2E]::Find('Groq Insert Dictation - Opname')
    $r = New-Object E2E+RECT; [E2E]::GetWindowRect($bubble, [ref]$r) | Out-Null
    [E2E]::Click([int](($r.Left + $r.Right) / 2), [int](($r.Top + $r.Bottom) / 2))
    Fg-Step 'after bubble click'
    Start-Sleep -Milliseconds 120
    Grab-Bubble 'processing'
    if (-not (Wait-Until { (Get-Content -LiteralPath $targetFile -Raw) -like '*Tweede dictaat*' } 12)) { throw "Second paste missing: $(Get-Content -LiteralPath $targetFile -Raw)" }
    Fg-Step 'after second paste'
    $result.target_text = [IO.File]::ReadAllText($targetFile)
    $result.audio = @(Select-String -Path "$profileDir\app.log" -Pattern 'Audio: ' | ForEach-Object { $_.Line })
} catch {
    $result.errors += $_.Exception.Message
} finally {
    if ($appProcess) { Stop-Process -Id $appProcess.Id -Force -ErrorAction SilentlyContinue }
    Stop-Process -Id $target.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    if (Test-Path "$profileDir\app.log") { Copy-Item "$profileDir\app.log" (Join-Path $Out 'qa-app.log') }
    Remove-Item -LiteralPath $profileDir -Recurse -Force -ErrorAction SilentlyContinue
    $result.finished = (Get-Date -Format o)
    $expected = 'Eerste dictaat via sneltoets. Tweede dictaat, gestopt met de bubbel. '
    $result.passed = ($result.errors.Count -eq 0) -and ($result.target_text -eq $expected) -and $result.bubble_noactivate -and -not ($result.foreground | Where-Object { -not $_.target })
    $result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $Out 'desktop-e2e.json') -Encoding UTF8
}
if (-not $result.passed) { exit 1 }
