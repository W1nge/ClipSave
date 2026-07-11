Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
scriptDir = fs.GetParentFolderName(WScript.ScriptFullName)
pythonw = scriptDir & "\.venv\Scripts\pythonw.exe"
appExe = scriptDir & "\ClipSave.exe"

If fs.FileExists(appExe) Then
    shell.Run """" & appExe & """", 1, False
ElseIf Not fs.FileExists(pythonw) Then
    MsgBox "缺少运行环境，请先在 ClipSave 目录运行 install.bat。", vbExclamation, "ClipSave"
Else
    shell.CurrentDirectory = scriptDir
    shell.Run """" & pythonw & """ """ & scriptDir & "\clipsave.py""", 0, False
End If
