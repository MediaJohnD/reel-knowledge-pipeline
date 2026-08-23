' Launches the reel-knowledge-pipeline webhook server with zero visible window -
' WshShell.Run's third argument (window style 0 = hidden) is what suppresses the
' console entirely; a plain "uv run ..." from a scheduled task still flashes a
' console window without this wrapper.
'
' Runs SYNCHRONOUSLY (bWaitOnReturn=True, the final argument below) rather than
' fire-and-forget: the scheduled task's own tracked process (wscript.exe) now lives
' and dies with the server, so Task Scheduler's "restart on failure" setting can
' actually detect a crash and restart it. A fire-and-forget launcher made that
' setting a no-op, since wscript.exe reported success the instant it spawned the
' detached child, regardless of whether the real server crashed minutes later.

Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
WshShell.CurrentDirectory = "C:\Users\media\Reel Knowledge Pipeline"

' Reap any server left over from a previous run before starting a new one.
'
' Stop-ScheduledTask kills wscript.exe (the process the task tracks) but NOT the
' cmd -> uv -> python chain underneath it, even with AllowHardTerminate=True -
' verified 2026-08-23, twice: a stop left cmd + two python processes alive with
' one still holding port 8787, so the next start would have failed to bind. The
' proper mechanism is a Job Object with KILL_ON_JOB_CLOSE, which VBScript cannot
' create; reaping on the way in gets the same practical result for a restart.
'
' Matched on the full command line, never on the image name: this machine runs
' many unrelated python.exe processes (Ollama, MiniBuffet, other tooling) and
' killing by name would take them all down.
reaped = 0
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set stale = wmi.ExecQuery( _
    "SELECT ProcessId, CommandLine FROM Win32_Process " & _
    "WHERE Name = 'python.exe' OR Name = 'cmd.exe' OR Name = 'uv.exe'")
For Each proc In stale
    If Not IsNull(proc.CommandLine) Then
        If InStr(proc.CommandLine, "reel_pipeline.cli serve-webhook") > 0 Then
            On Error Resume Next  ' a process that exited between query and kill is fine
            proc.Terminate()
            On Error Goto 0
            reaped = reaped + 1
        End If
    End If
Next
' Terminate() returns before the OS has released the listening socket; without a
' beat here the fresh server can still lose the bind to its own predecessor.
If reaped > 0 Then WScript.Sleep 3000

logPath = "data\logs\webhook_stdout.log"
maxBytes = 5000000 ' webhook_stdout.log is raw cmd.exe redirection, outside the JSON
                    ' logging pipeline that pipeline.log gets rotation from (see
                    ' logging_setup.py) - needs its own simple size cap here instead.
If fso.FileExists(logPath) Then
    If fso.GetFile(logPath).Size > maxBytes Then
        oldPath = logPath & ".1"
        If fso.FileExists(oldPath) Then fso.DeleteFile(oldPath)
        fso.MoveFile logPath, oldPath
    End If
End If

If reaped > 0 Then
    Set logFile = fso.OpenTextFile(logPath, 8, True)
    logFile.WriteLine "[launcher] reaped " & reaped & " orphaned serve-webhook process(es) before start"
    logFile.Close
End If

WshShell.Run "cmd /c uv run python -m reel_pipeline.cli serve-webhook >> data\logs\webhook_stdout.log 2>&1", 0, True
