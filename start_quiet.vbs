' Launch VANTA with no console window -- just the UI.
'
' Run(cmd, 0, True) hides the window and waits for the exit code. Silent on
' success is the whole point; silent on FAILURE would be worse than the
' console, because a launch that does nothing and says nothing is
' indistinguishable from one that is still starting. So a non-zero exit
' surfaces a dialog pointing at the log.

Dim shell, fso, here, batch, logFile, code
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

here = fso.GetParentFolderName(WScript.ScriptFullName)
batch = here & "\start_quiet.bat"
logFile = here & "\vault\logs\winter.log"

If Not fso.FileExists(batch) Then
    MsgBox "Cannot find start_quiet.bat in " & here, 16, "VANTA"
    WScript.Quit 1
End If

shell.CurrentDirectory = here
code = shell.Run("""" & batch & """", 0, True)

If code <> 0 Then
    Dim reason
    If code = 2 Then
        reason = "Identity verification was closed, aborted, or could not complete. No VANTA service was loaded."
    Else
        reason = "A required startup service could not be initialized."
    End If
    MsgBox "VANTA did not start (exit code " & code & ")." & vbCrLf & vbCrLf & _
           reason & vbCrLf & _
           "Details are in:" & vbCrLf & logFile, 16, "VANTA"
End If
