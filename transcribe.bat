@echo off
set WAVFILE=%TEMP%\whisper_ptt.wav
set OUTFILE=%TEMP%\whisper_out.txt
set WHISPER=C:\Users\admin\scoop\apps\whisper-cpp\current\whisper-cli.exe
set MODEL=C:\Users\admin\scoop\apps\whisper-cpp\current\models\ggml-base.en.bin

"%WHISPER%" -m "%MODEL%" -f "%WAVFILE%" -np -nt > "%OUTFILE%" 2>&1
