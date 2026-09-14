' ===========================================================================
'  VibeMote launcher  (PURE ASCII ON PURPOSE - DO NOT ADD CHINESE HERE)
' ---------------------------------------------------------------------------
'  Why ASCII only: wscript reads .vbs using the system ANSI codepage (GBK on
'  Chinese Windows). If this file contains UTF-8 Chinese comments, the bytes
'  get misread and the parser dies with "Unterminated string constant"
'  (800A0409). Line endings must be CRLF too.
'
'  What it does: find a pythonw.exe and start app.py with a HIDDEN window
'  (window style 0), so the user never sees a black console box.
'  Double-click  -> starts the app, waits until the console answers, opens browser
'  --silent      -> start only, no browser (used by the autostart entry)
'
'  Why it waits and checks (added 2026-09-13): app.py runs under pythonw.exe,
'  where stdout is None - so any startup failure is COMPLETELY silent: no
'  window, no error, no browser, nothing. The user report was literally
'  "I extracted it and double-click does nothing". Now the launcher polls
'  http://127.0.0.1:<port>/api/state and, if the app never answers, shows a
'  message and opens client.log in Notepad so there is always a clue.
' ===========================================================================
Option Explicit

Dim fso, sh, base, py, cmd, i, silent, boot, check, port, ok, tries, before, now

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

base = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = base

silent = False
boot = False
check = False
For i = 0 To WScript.Arguments.Count - 1
  If LCase(WScript.Arguments(i)) = "--silent" Then silent = True
  ' --boot = this copy was started by the BOOT AUTOSTART entry, not by a user
  ' double-click. app.py uses it to suppress the automatic "install the virtual
  ' audio cable" UAC prompt: at boot nobody is at the machine, so the prompt
  ' would just sit there - and it would come back on every single boot.
  ' (Keep this file pure ASCII + CRLF: wscript mis-parses non-ASCII comments.)
  If LCase(WScript.Arguments(i)) = "--boot" Then boot = True
  ' --check = parse the arguments, find an interpreter, print one line, exit.
  ' Used by the automated test and by the packagers (_package.py / _build_offline.py)
  ' so a broken launcher can never be shipped again: this whole block runs under
  ' "Option Explicit", so forgetting to Dim a new variable kills the launcher with
  ' 800A01F4 at the *assignment* - which is exactly what happened once, and it was
  ' only noticed after the package had been published.
  If LCase(WScript.Arguments(i)) = "--check" Then check = True
Next

If check Then silent = True      ' never pop a MsgBox that would block an automated run

py = PickPy()
If check Then
  WScript.Echo "VBS-CHECK-OK silent=" & silent & " boot=" & boot & " py=" & py
  WScript.Quit 0
End If
If Len(py) = 0 Then
  If Not silent Then
    MsgBox "No Python found." & vbCrLf & vbCrLf & _
           "Install Python 3.10+ from python.org and TICK 'Add python.exe to PATH'." & vbCrLf & _
           "Then run this file again." & vbCrLf & vbCrLf & _
           "(Or use the offline package, which carries its own Python.)", 16, "VibeMote"
  End If
  WScript.Quit 1
End If

port = ReadPort()

' Remember the ready marker's timestamp. We must NOT delete it: if a copy of the
' app is ALREADY running, the instance we launch will hit the single-instance
' lock, exit immediately, and never write a new marker - deleting the marker
' would make that case look like a failed start (and re-clicking the icon while
' it runs is the most common double-click scenario of all).
before = MarkerStamp()

' app.py must NOT open the browser itself: we do it only after the console
' really answers, so a failed start can never look like "nothing happened".
cmd = """" & py & """ """ & base & "\app.py"" --silent"
If boot Then cmd = cmd & " --boot"
If port > 0 Then cmd = cmd & " --port " & port

On Error Resume Next
sh.Run cmd, 0, False
If Err.Number <> 0 Then
  If Not silent Then
    MsgBox "Failed to start:" & vbCrLf & Err.Description & vbCrLf & vbCrLf & _
           "Tried interpreter: " & py, 16, "VibeMote"
  End If
  Err.Clear
  WScript.Quit 1
End If
On Error GoTo 0

' Two independent signals, and the cheap one is the hot path:
'   * ready marker is NEW  -> plain file check (instant), used for a fresh start
'   * console answers HTTP -> needed for "another copy is already running",
'     because that instance exits immediately and never writes a new marker
' Probing HTTP only ONCE matters: CreateObject("MSXML2.XMLHTTP") + Send goes
' through WinINet (system proxy lookup) and measured ~2s per call here, so
' polling it 30 times made a failed start take 82 seconds. The marker loop is
' 25 * 700ms = ~17s, which is plenty for a cold Bluetooth/audio stack.
ok = False
If ServerUp(port) Then
  ok = True
Else
  For tries = 1 To 25
    now = MarkerStamp()
    If Len(now) > 0 And now <> before Then
      ok = True
      port = ReadyPort(port)
      Exit For
    End If
    WScript.Sleep 700
  Next
End If

If ok Then
  If Not silent Then sh.Run "http://127.0.0.1:" & port & "/", 1, False
  WScript.Quit 0
End If

' Not up. Common causes: another client holds the single-instance lock, or the
' interpreter/deps are broken. Either way client.log now has the reason.
If Not silent Then
  MsgBox "VibeMote could not start." & vbCrLf & vbCrLf & _
         "No answer from http://127.0.0.1:" & port & "/" & vbCrLf & _
         "client.log will be opened - the last lines say why." & vbCrLf & vbCrLf & _
         "(Another copy already running is the most common cause.)", 48, "VibeMote"
  If fso.FileExists(base & "\client.log") Then
    sh.Run "notepad.exe """ & base & "\client.log""", 1, False
  End If
End If
WScript.Quit 1


' The port out of the ready marker (its first line). Keeps the old value on failure.
Function ReadyPort(fallback)
  Dim ts, s
  ReadyPort = fallback
  On Error Resume Next
  Set ts = fso.OpenTextFile(base & "\_ready.txt", 1)
  If Err.Number = 0 Then
    If Not ts.AtEndOfStream Then
      s = Trim(ts.ReadLine)
      If Len(s) > 0 Then
        If IsNumeric(s) Then
          If CLng(s) > 0 And CLng(s) < 65536 Then ReadyPort = CLng(s)
        End If
      End If
    End If
    ts.Close
  End If
  Err.Clear
  On Error GoTo 0
End Function


' Read the UI port from config.json (users can change it). 0 = not found.
Function ReadPort()
  Dim p, ts, txt, k, n, s, c
  ReadPort = 8787
  p = base & "\config.json"
  If Not fso.FileExists(p) Then Exit Function
  On Error Resume Next
  Set ts = fso.OpenTextFile(p, 1)
  If Err.Number <> 0 Then
    Err.Clear
    On Error GoTo 0
    Exit Function
  End If
  txt = ts.ReadAll
  ts.Close
  On Error GoTo 0
  k = InStr(txt, """ui_port""")
  If k = 0 Then Exit Function
  ' take the first run of digits after the key
  n = 0
  s = ""
  For n = k + 9 To Len(txt)
    c = Mid(txt, n, 1)
    If c >= "0" And c <= "9" Then
      s = s & c
    ElseIf Len(s) > 0 Then
      Exit For
    End If
  Next
  If Len(s) > 0 Then ReadPort = CLng(s)
  If ReadPort < 1 Or ReadPort > 65535 Then ReadPort = 8787
End Function


' Timestamp of the ready marker, or "" when it does not exist yet.
Function MarkerStamp()
  MarkerStamp = ""
  On Error Resume Next
  If fso.FileExists(base & "\_ready.txt") Then
    MarkerStamp = CStr(CDbl(fso.GetFile(base & "\_ready.txt").DateLastModified))
  End If
  Err.Clear
  On Error GoTo 0
End Function


' Is the console answering yet?
' Prefer ServerXMLHTTP (WinHTTP: no system-proxy auto-detect, supports
' timeouts); fall back to plain XMLHTTP. Returns False if neither is available,
' which just means the marker-file path is used instead.
Function ServerUp(p)
  Dim h
  ServerUp = False
  On Error Resume Next
  Set h = CreateObject("MSXML2.ServerXMLHTTP.6.0")
  If Err.Number <> 0 Then
    Err.Clear
    Set h = CreateObject("MSXML2.XMLHTTP")
  End If
  If Err.Number <> 0 Then
    Err.Clear
    On Error GoTo 0
    Exit Function
  End If
  h.setTimeouts 800, 800, 800, 1500
  h.Open "GET", "http://127.0.0.1:" & p & "/api/state", False
  h.Send
  If Err.Number = 0 Then
    If h.Status = 200 Then ServerUp = True
  End If
  Err.Clear
  On Error GoTo 0
End Function


Function PickPy()
  Dim cands, roots, i, s, f, p, ts, line

  ' 0) explicit override: python.txt next to this script holds ONE line with the
  '    full path to pythonw.exe. Useful when the machine already has a prepared
  '    environment (or when the stick carries a private runtime).
  p = base & "\python.txt"
  If fso.FileExists(p) Then
    On Error Resume Next
    Set ts = fso.OpenTextFile(p, 1)
    If Err.Number = 0 Then
      If Not ts.AtEndOfStream Then
        line = Trim(ts.ReadLine)
        If Len(line) > 0 And fso.FileExists(line) Then
          ts.Close
          PickPy = line
          Exit Function
        End If
      End If
      ts.Close
    End If
    Err.Clear
    On Error GoTo 0
  End If

  ' 1) package-local virtualenv (created by the "install dependencies" button).
  '    It must be a COMPLETE venv. A half-deleted .venv still has
  '    Scripts\pythonw.exe lying around but cannot run anything, and picking it
  '    makes double-click do absolutely nothing (no window, no error, no browser)
  '    - which is exactly the "uninstalled it and now it will not start" report.
  '    A real venv always has Scripts\python.exe + pyvenv.cfg, so check both.
  p = base & "\.venv\Scripts\pythonw.exe"
  If fso.FileExists(p) Then
    If fso.FileExists(base & "\.venv\Scripts\python.exe") And _
       fso.FileExists(base & "\.venv\pyvenv.cfg") Then
      PickPy = p : Exit Function
    End If
  End If

  ' 2) a python runtime shipped next to the app (offline / USB layout)
  p = base & "\runtime\pythonw.exe"
  If fso.FileExists(p) Then PickPy = p : Exit Function

  ' 3) common per-user and machine-wide installations
  cands = Array( _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe"), _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"), _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"), _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"), _
    "C:\Python313\pythonw.exe", "C:\Python312\pythonw.exe", _
    "C:\Python311\pythonw.exe", "C:\Python310\pythonw.exe")
  For i = 0 To UBound(cands)
    If fso.FileExists(cands(i)) Then PickPy = cands(i) : Exit Function
  Next

  ' 4) scan one level of subdirectories under the usual Python roots
  roots = Array( _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python"), _
    "C:\", "D:\DEV")
  For Each s In roots
    If fso.FolderExists(s) Then
      For Each f In fso.GetFolder(s).SubFolders
        If fso.FileExists(f.Path & "\pythonw.exe") Then
          PickPy = f.Path & "\pythonw.exe" : Exit Function
        End If
        If fso.FileExists(f.Path & "\Scripts\pythonw.exe") Then
          PickPy = f.Path & "\Scripts\pythonw.exe" : Exit Function
        End If
      Next
    End If
  Next

  ' 5) last resort: whatever is on PATH
  PickPy = "pythonw.exe"
End Function
