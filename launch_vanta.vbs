Option Explicit

' Launch the normal bootstrap invisibly. start.bat remains available when a
' visible diagnostic console is needed, while the desktop shortcut shows only
' VANTA's own interface.
Dim shell
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "D:\winter"
shell.Run Chr(34) & "D:\winter\start.bat" & Chr(34), 0, False
Set shell = Nothing
