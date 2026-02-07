#Requires AutoHotkey v2.0
#SingleInstance Force
#Warn All, Off

isRecording := false

F9:: {
    global isRecording

    if !isRecording {
        isRecording := true

        ; Start recording batch (runs ffmpeg)
        Run('cmd /c "C:\Users\admin\Documents\Claude\whisper-ptt\record.bat"',, "Hide")
        ToolTip("🎤")
    }
}

F9 up:: {
    global isRecording

    if isRecording {
        isRecording := false
        ToolTip("⏳")

        ; Kill ffmpeg to stop recording
        Run("taskkill /F /IM ffmpeg.exe",, "Hide")
        Sleep(500)

        wavFile := A_Temp "\whisper_ptt.wav"
        outFile := A_Temp "\whisper_out.txt"

        ; Check file size
        if !FileExist(wavFile) || FileGetSize(wavFile) < 1000 {
            ToolTip("❌ No audio")
            SetTimer(() => ToolTip(), -2000)
            return
        }

        ; Transcribe
        RunWait('cmd /c "C:\Users\admin\Documents\Claude\whisper-ptt\transcribe.bat"',, "Hide")

        if FileExist(outFile) {
            text := Trim(FileRead(outFile))
            try FileDelete(outFile)

            if text != "" && !InStr(text, "error") {
                SendText(text)
                ToolTip("✓")
            } else {
                ToolTip("❌ Empty")
            }
        } else {
            ToolTip("❌ No txt")
        }

        SetTimer(() => ToolTip(), -1500)
    }
}

Esc::ExitApp()

ToolTip("PTT: F9")
SetTimer(() => ToolTip(), -2000)
