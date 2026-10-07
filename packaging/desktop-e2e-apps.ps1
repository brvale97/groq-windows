<#
Paste check of a built folder build into real applications: Word, Edge and Chrome.

Same isolated "qa" profile and offline QA transcripts as desktop-e2e.ps1. For
every target the shortcut starts a recording from the real microphone; Word is
stopped with the shortcut, the browsers with a click on the status bubble. The
pasted text is read back from the application itself. Browsers use a throwaway
profile; Word gets a new document that is closed without saving.

  powershell -ExecutionPolicy Bypass -File desktop-e2e-apps.ps1 -App <folder>\GroqInsertDictation.exe -Out <folder>
Must run in an unlocked interactive session.
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
Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices; using System.Text; using System.Collections.Generic;
public static class Windows2 {
    public delegate bool EnumProc(IntPtr h, IntPtr p);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc f, IntPtr p);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
    public static IntPtr FindPrefix(string prefix) {
        IntPtr found = IntPtr.Zero;
        EnumWindows((h, p) => { var s = new StringBuilder(512); GetWindowText(h, s, 512);
            if (IsWindowVisible(h) && s.ToString().StartsWith(prefix)) { found = h; return false; } return true; }, IntPtr.Zero);
        return found;
    }
    public static string Title(IntPtr h) { var s = new StringBuilder(1024); GetWindowText(h, s, 1024); return s.ToString(); }
}
'@
$texts = [ordered]@{ word = 'Word plaktest Groq.'; edge = 'Edge plaktest Groq.'; chrome = 'Chrome plaktest Groq.' }
$result = [ordered]@{ started = (Get-Date -Format o); targets = [ordered]@{} }
$profileDir = Join-Path $env:APPDATA 'GroqInsertDictation-qa'
Remove-Item -LiteralPath $profileDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $profileDir | Out-Null
'{"shortcut": "ctrl+alt+shift+f24", "autostart": false, "input_device": "", "language": "nl"}' | Set-Content -LiteralPath "$profileDir\settings.json" -Encoding ASCII
$env:GROQ_DICTATION_PROFILE = 'qa'
$env:GROQ_DICTATION_QA_TRANSCRIPTS = ($texts.Values -join '|')
function Wait-Until([scriptblock]$Condition, [double]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) { if (& $Condition) { return $true }; Start-Sleep -Milliseconds 150 }
    return [bool](& $Condition)
}
function Log-Count([string]$Pattern) { if (Test-Path "$profileDir\app.log") { @(Select-String -Path "$profileDir\app.log" -Pattern $Pattern -SimpleMatch).Count } else { 0 } }
function Focus([IntPtr]$Hwnd) {
    [E2E]::Key(0x12, $false); [E2E]::Key(0x12, $true)
    [E2E]::SetForegroundWindow($Hwnd) | Out-Null
    return (Wait-Until { [E2E]::Foreground() -eq [E2E]::GetAncestor($Hwnd, 2).ToInt64() } 4)
}
function Dictate([IntPtr]$Hwnd, [string]$Stop) {
    $root = [E2E]::GetAncestor($Hwnd, 2).ToInt64()
    $count = Log-Count 'Opname gestart'
    $done = Log-Count 'Klaar. Gebruik je shortcut'
    [E2E]::Hotkey()
    if (-not (Wait-Until { (Log-Count 'Opname gestart') -gt $count } 6)) { throw 'Shortcut did not start a recording' }
    Start-Sleep -Milliseconds 1700
    $during = [E2E]::Foreground() -eq $root
    if ($Stop -eq 'bubble') {
        $bubble = [E2E]::Find('Groq Insert Dictation - Opname')
        $r = New-Object E2E+RECT; [E2E]::GetWindowRect($bubble, [ref]$r) | Out-Null
        [E2E]::Click([int](($r.Left + $r.Right) / 2), [int](($r.Top + $r.Bottom) / 2))
    } else { [E2E]::Hotkey() }
    $afterStop = [E2E]::Foreground() -eq $root
    Wait-Until { (Log-Count 'Klaar. Gebruik je shortcut') -gt $done } 10 | Out-Null
    Start-Sleep -Milliseconds 700
    return @{ focus_while_recording = $during; focus_after_stop = $afterStop; stop = $Stop; focus_after_paste = ([E2E]::Foreground() -eq $root) }
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
$appProcess = Start-Process -FilePath $App -WorkingDirectory (Split-Path $App) -PassThru
try {
    if (-not (Wait-Until { (Log-Count 'Startup ready') -ge 1 } 30)) { throw 'App did not finish startup' }
    $result.closed_first_run_settings = Close-FirstRunSettings

    # Word: new document through COM, read back from the document, closed without saving.
    $entry = [ordered]@{}
    $word = $null
    try {
        $word = New-Object -ComObject Word.Application
        $word.Visible = $true
        $doc = $word.Documents.Add()
        $word.Activate()
        Start-Sleep -Seconds 2
        $hwnd = [IntPtr]$word.ActiveWindow.Hwnd
        if (-not (Focus $hwnd)) { throw 'Word did not get the focus' }
        $entry += Dictate $hwnd 'shortcut'
        Shot-Window $hwnd (Join-Path $Out 'word-after-paste.png')
        $entry.documents = $word.Documents.Count
        $entry.text = $word.ActiveDocument.Content.Text.Trim()
        $entry.passed = ($entry.text -eq $texts.word) -and $entry.focus_while_recording -and $entry.focus_after_stop
        $doc.Close(0)
    } catch { $entry.error = $_.Exception.Message; $entry.passed = $false }
    finally {
        if ($word) {
            $wordPid = [E2E]::ForegroundPid()
            try { $word.Quit(0) } catch {}
            [Runtime.InteropServices.Marshal]::ReleaseComObject($word) | Out-Null
            # Quit() can leave the automation instance running; stop only the one this test started.
            Start-Sleep -Seconds 2
            Get-CimInstance Win32_Process -Filter "Name = 'WINWORD.EXE'" | Where-Object { $_.CommandLine -like '*/Automation*' -and $_.ProcessId -eq $wordPid } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
        }
    }
    $result.targets.word = $entry

    # Browsers: a local page mirrors its text box into the window title.
    $page = Join-Path $Out 'target.html'
    '<!doctype html><title>E2E:</title><textarea id=t autofocus style="width:96vw;height:80vh;font:16px Segoe UI" oninput="document.title=''E2E:''+this.value"></textarea>' | Set-Content -LiteralPath $page -Encoding UTF8
    $browsers = [ordered]@{
        edge = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
        chrome = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
    }
    foreach ($name in $browsers.Keys) {
        $entry = [ordered]@{}
        $profile = Join-Path $Out "$name-profile"
        $proc = $null
        try {
            Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue
            $proc = Start-Process -FilePath $browsers[$name] -ArgumentList "--user-data-dir=`"$profile`"", '--no-first-run', '--no-default-browser-check', '--window-size=800,500', '--window-position=120,120', "--app=file:///$($page.Replace('\','/'))" -PassThru
            if (-not (Wait-Until { [Windows2]::FindPrefix('E2E:') -ne [IntPtr]::Zero } 20)) { throw "$name window did not open" }
            $hwnd = [Windows2]::FindPrefix('E2E:')
            Start-Sleep -Seconds 1
            if (-not (Focus $hwnd)) { throw "$name did not get the focus" }
            $entry += Dictate $hwnd 'bubble'
            Shot-Window $hwnd (Join-Path $Out "$name-after-paste.png")
            $title = [Windows2]::Title($hwnd)
            $entry.text = $title.Substring(4).Trim()
            $entry.passed = ($entry.text -eq $texts[$name]) -and $entry.focus_while_recording -and $entry.focus_after_stop
        } catch { $entry.error = $_.Exception.Message; $entry.passed = $false }
        finally {
            Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*$profile*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Seconds 1
            Remove-Item -LiteralPath $profile -Recurse -Force -ErrorAction SilentlyContinue
        }
        $result.targets[$name] = $entry
    }
} finally {
    Stop-Process -Id $appProcess.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500
    if (Test-Path "$profileDir\app.log") { Copy-Item "$profileDir\app.log" (Join-Path $Out 'qa-app.log') -Force }
    Remove-Item -LiteralPath $profileDir -Recurse -Force -ErrorAction SilentlyContinue
    $result.finished = (Get-Date -Format o)
    $result.passed = -not ($result.targets.Values | Where-Object { -not $_.passed })
    $result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $Out 'apps-e2e.json') -Encoding UTF8
}
if (-not $result.passed) { exit 1 }
