Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
root = fs.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = root
python = root & "\.venv\Scripts\pythonw.exe"
If Not fs.FileExists(python) Then
  MsgBox "Run SETUP_PERSONAL.cmd first.", 64, "FlowEngine Personal"
  WScript.Quit
End If
shell.Run Chr(34) & python & Chr(34) & " -m flowengine.desktop", 0, False
