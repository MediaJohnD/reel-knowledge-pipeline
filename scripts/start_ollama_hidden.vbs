' Launches Ollama (used for local enrichment/vision) with zero visible window,
' bound to the port this pipeline expects (see REEL_OLLAMA_HOST in .env).
'
' This exists because Ollama is not started by its own auto-launching tray app
' in a way that survives a reboot bound to the non-default port this pipeline
' uses (11435, chosen to avoid colliding with any other app's default-port
' Ollama instance) - without this, enrichment/vision requests fail with
' connection-refused until someone notices and starts it manually.
'
' Runs ASYNCHRONOUSLY (bWaitOnReturn=False), unlike start_webhook_hidden.vbs -
' this script is registered as the FIRST action on the same ReelPipelineWebhook
' scheduled task, ahead of the webhook launcher, which itself runs
' synchronously to stay Task-Scheduler-trackable for crash recovery. If this
' script also blocked, it would never let the webhook action run at all.
' Trade-off: Task Scheduler's restart-on-failure for this task is keyed off the
' webhook process, so it relaunches Ollama too whenever the whole task
' restarts (e.g. webhook crashed) or at login/reboot - but it will NOT notice
' or recover from Ollama dying on its own while the webhook server stays
' healthy. That gap needs a separate, independently-tracked scheduled task to
' close, which requires task *creation* rights this environment didn't have
' when this was set up (2026-07-16) - modifying the existing task worked,
' registering a brand new one didn't.
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
WshShell.CurrentDirectory = "C:\Users\media\Reel Knowledge Pipeline"

logPath = "data\logs\ollama_stdout.log"
maxBytes = 5000000
If fso.FileExists(logPath) Then
    If fso.GetFile(logPath).Size > maxBytes Then
        oldPath = logPath & ".1"
        If fso.FileExists(oldPath) Then fso.DeleteFile(oldPath)
        fso.MoveFile logPath, oldPath
    End If
End If

WshShell.Environment("Process")("OLLAMA_HOST") = "127.0.0.1:11435"
WshShell.Run "cmd /c ""C:\Users\media\AppData\Local\Programs\Ollama\ollama.exe"" serve >> data\logs\ollama_stdout.log 2>&1", 0, False
