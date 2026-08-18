' Runs launch.bat with no visible console window, so the app opens like a normal desktop app.
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)

Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = scriptDir
shell.Run """" & scriptDir & "\launch.bat""", 0, False
