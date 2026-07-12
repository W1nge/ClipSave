Option Explicit

Dim shell, fs, scriptDir, appExe, launchResult

Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
scriptDir = fs.GetParentFolderName(WScript.ScriptFullName)
appExe = scriptDir & "\ClipSave.exe"

If Not fs.FileExists(appExe) Then
    MsgBox "ClipSave.exe was not found. Download the complete release or run build.bat first.", vbExclamation, "ClipSave release launcher"
    WScript.Quit 1
End If

shell.CurrentDirectory = scriptDir
On Error Resume Next
launchResult = shell.Run("""" & appExe & """", 1, False)
If Err.Number <> 0 Then
    MsgBox "Unable to start ClipSave.exe: " & Err.Description, vbCritical, "ClipSave release launcher"
    WScript.Quit 1
End If
On Error GoTo 0

WScript.Quit launchResult
