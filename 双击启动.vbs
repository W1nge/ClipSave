Option Explicit

Dim shell, fs, scriptDir, appDir, internalDir, appExe, launchResult, folder, file, hasVersionedPython

Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
scriptDir = fs.GetParentFolderName(WScript.ScriptFullName)
appDir = scriptDir & "\ClipSave"
internalDir = appDir & "\_internal"
appExe = appDir & "\ClipSave.exe"

If Not fs.FileExists(appExe) Then
    MsgBox "ClipSave.exe was not found. Download the complete release or run build.bat first.", vbExclamation, "ClipSave release launcher"
    WScript.Quit 1
End If

If Not fs.FolderExists(internalDir) Then
    MsgBox "ClipSave runtime directory (_internal) is missing. Download and extract the complete release.", vbExclamation, "ClipSave release launcher"
    WScript.Quit 1
End If

If Not fs.FileExists(internalDir & "\base_library.zip") Or _
   Not fs.FileExists(internalDir & "\python3.dll") Or _
   Not fs.FileExists(internalDir & "\PySide6\Qt6Core.dll") Or _
   Not fs.FileExists(internalDir & "\PySide6\Qt6Gui.dll") Or _
   Not fs.FileExists(internalDir & "\PySide6\Qt6Widgets.dll") Then
    MsgBox "ClipSave runtime files are incomplete. Download and extract the complete release.", vbExclamation, "ClipSave release launcher"
    WScript.Quit 1
End If

hasVersionedPython = False
Set folder = fs.GetFolder(internalDir)
For Each file In folder.Files
    If LCase(Left(file.Name, 6)) = "python3" And LCase(file.Name) <> "python3.dll" And LCase(Right(file.Name, 4)) = ".dll" Then
        hasVersionedPython = True
        Exit For
    End If
Next
If Not hasVersionedPython Then
    MsgBox "ClipSave's versioned Python runtime is missing. Download and extract the complete release.", vbExclamation, "ClipSave release launcher"
    WScript.Quit 1
End If

shell.CurrentDirectory = fs.GetParentFolderName(appExe)
On Error Resume Next
launchResult = shell.Run("""" & appExe & """", 1, False)
If Err.Number <> 0 Then
    MsgBox "Unable to start ClipSave.exe: " & Err.Description, vbCritical, "ClipSave release launcher"
    WScript.Quit 1
End If
On Error GoTo 0

WScript.Quit launchResult
