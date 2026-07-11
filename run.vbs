Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
scriptDir = fs.GetParentFolderName(WScript.ScriptFullName)
appExe = scriptDir & "\ClipSave.exe"
pythonw = scriptDir & "\.venv\Scripts\pythonw.exe"

If fs.FileExists(appExe) Then
    shell.Run """" & appExe & """", 1, False
ElseIf fs.FileExists(pythonw) Then
    shell.CurrentDirectory = scriptDir
    shell.Run """" & pythonw & """ """ & scriptDir & "\clipsave.py""", 0, False
Else
    MsgBox "ClipSave is not installed. Run install.bat first.", vbExclamation, "ClipSave"
End If
