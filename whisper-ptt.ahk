#Requires AutoHotkey v2.0
#SingleInstance Force
#Warn All, Off

isRecording := false

RCtrl:: {
    global isRecording

    if !isRecording {
        isRecording := true

        ; Start recording batch (runs ffmpeg)
        Run('cmd /c "' A_ScriptDir '\record.bat"',, "Hide")
        ToolTip("REC")
    }
}

RCtrl up:: {
    global isRecording

    if isRecording {
        isRecording := false
        ToolTip("TRANSCRIBE")

        ; Kill ffmpeg to stop recording
        Run("taskkill /F /IM ffmpeg.exe",, "Hide")
        Sleep(500)

        wavFile := A_Temp "\whisper_ptt.wav"
        outFile := A_Temp "\whisper_out.txt"

        ; Check file size
        if !FileExist(wavFile) || FileGetSize(wavFile) < 1000 {
            ToolTip("NO AUDIO")
            SetTimer(() => ToolTip(), -2000)
            return
        }

        ; Transcribe
        RunWait('cmd /c "' A_ScriptDir '\transcribe.bat"',, "Hide")

        if FileExist(outFile) {
            text := Trim(FileRead(outFile))
            try FileDelete(outFile)

            if text != "" && !InStr(text, "error") {
                SendText(text)
                ToolTip("OK")
            } else {
                ToolTip("EMPTY")
            }
        } else {
            ToolTip("NO TXT")
        }

        SetTimer(() => ToolTip(), -1500)
    }
}

Esc::ExitApp()

ToolTip("PTT: RCtrl")
SetTimer(() => ToolTip(), -2000)
