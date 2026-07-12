Option Explicit

Dim shell, fs, scriptDir, pythonw, sourceEntry, launchResult

Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
scriptDir = fs.GetParentFolderName(WScript.ScriptFullName)
pythonw = scriptDir & "\.venv\Scripts\pythonw.exe"
sourceEntry = scriptDir & "\clipsave.py"

If Not fs.FileExists(pythonw) Then
    MsgBox "Source environment not found. Run install.bat first.", vbExclamation, "ClipSave source launcher"
    WScript.Quit 1
End If

If Not fs.FileExists(sourceEntry) Then
    MsgBox "Source entry point not found: clipsave.py", vbCritical, "ClipSave source launcher"
    WScript.Quit 1
End If

shell.CurrentDirectory = scriptDir
On Error Resume Next
launchResult = shell.Run("""" & pythonw & """ """ & sourceEntry & """", 0, False)
If Err.Number <> 0 Then
    MsgBox "Unable to start ClipSave from source: " & Err.Description, vbCritical, "ClipSave source launcher"
    WScript.Quit 1
End If
On Error GoTo 0

WScript.Quit launchResult
