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

WshShell.Run "cmd /c uv run python -m reel_pipeline.cli serve-webhook >> data\logs\webhook_stdout.log 2>&1", 0, True
