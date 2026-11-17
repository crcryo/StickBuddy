"""串口监听：把 StickBuddy 固件的串口日志写到文件，Ctrl+C 或删停止文件结束"""
import serial, time, sys, os

STOP = "D:\\Tools\\StickBuddy\\serialmon.stop"
OUT = "D:\\Tools\\StickBuddy\\fw_serial.log"
port = sys.argv[1] if len(sys.argv) > 1 else "COM5"

s = serial.Serial(port, 115200, timeout=1)
print("listening", port, flush=True)
with open(OUT, "ab") as f:
    while not os.path.exists(STOP):
        data = s.read(1024)
        if data:
            f.write(data)
            f.flush()
            sys.stdout.write(data.decode("utf-8", "replace"))
            sys.stdout.flush()
    s.close()
    os.remove(STOP)
