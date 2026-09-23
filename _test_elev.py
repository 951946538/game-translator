"""独立验证 _process_elevated 权限检测逻辑（与 main.py 中实现保持一致）"""
import ctypes
import ctypes.wintypes as wintypes

kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32
kernel32.OpenProcess.restype = wintypes.HANDLE


def process_elevated(pid):
    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    try:
        h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            token = wintypes.HANDLE()
            if not advapi32.OpenProcessToken(h, 0x0008, ctypes.byref(token)):
                return None
            try:
                elev = wintypes.DWORD()
                ret_len = wintypes.DWORD()
                if advapi32.GetTokenInformation(token, 20, ctypes.byref(elev), 4, ctypes.byref(ret_len)):
                    return bool(elev.value)
            finally:
                kernel32.CloseHandle(token)
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return None
    return None


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    user32 = ctypes.windll.user32

    # explorer（任务栏）：普通权限，应返回 False
    pid_exp = wintypes.DWORD()
    hwnd = user32.FindWindowW("Shell_TrayWnd", None)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_exp))
    print("explorer (期望 False):", process_elevated(pid_exp.value))

    # 不存在的 PID：应返回 None
    print("无效 PID (期望 None):", process_elevated(99999999))

    # 当前进程
    cur = kernel32.GetCurrentProcessId()
    r = process_elevated(cur)
    print("当前进程:", r, "(None=无法判断/权限受限也属正常)")
