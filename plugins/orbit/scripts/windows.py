"""Windows support for Orbit, using only the standard library (ctypes).

Built-in terminals use ConPTY, the pseudo console in Windows 10 version 1809 and later. Process checks
use OpenProcess, because os.kill(pid, 0) on Windows does not test a process: it sends it Ctrl+C.
"""
import ctypes
import os
import subprocess
import threading
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100
PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 259
INFINITE = 0xFFFFFFFF


class COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD), ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE), ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


def _fn(name, restype, *argtypes):
    f = getattr(kernel32, name, None)
    if f is not None:
        f.restype, f.argtypes = restype, list(argtypes)
    return f


H, D, B, P = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, ctypes.c_void_p
CreatePipe = _fn("CreatePipe", B, ctypes.POINTER(H), ctypes.POINTER(H), P, D)
CreatePseudoConsole = _fn("CreatePseudoConsole", ctypes.c_long, COORD, H, H, D, ctypes.POINTER(H))
ResizePseudoConsole = _fn("ResizePseudoConsole", ctypes.c_long, H, COORD)
ClosePseudoConsole = _fn("ClosePseudoConsole", None, H)
InitializeProcThreadAttributeList = _fn("InitializeProcThreadAttributeList", B, P, D, D, ctypes.POINTER(ctypes.c_size_t))
UpdateProcThreadAttribute = _fn("UpdateProcThreadAttribute", B, P, D, ctypes.c_size_t, P, ctypes.c_size_t, P, P)
DeleteProcThreadAttributeList = _fn("DeleteProcThreadAttributeList", None, P)
CreateProcessW = _fn("CreateProcessW", B, wintypes.LPCWSTR, wintypes.LPWSTR, P, P, B, D, P, wintypes.LPCWSTR,
                     ctypes.POINTER(STARTUPINFOEXW), ctypes.POINTER(PROCESS_INFORMATION))
ReadFile = _fn("ReadFile", B, H, P, D, ctypes.POINTER(D), P)
WriteFile = _fn("WriteFile", B, H, P, D, ctypes.POINTER(D), P)
CloseHandle = _fn("CloseHandle", B, H)
WaitForSingleObject = _fn("WaitForSingleObject", D, H, D)
GetExitCodeProcess = _fn("GetExitCodeProcess", B, H, ctypes.POINTER(D))
TerminateProcess = _fn("TerminateProcess", B, H, wintypes.UINT)
OpenProcess = _fn("OpenProcess", H, D, B, D)
QueryFullProcessImageNameW = _fn("QueryFullProcessImageNameW", B, H, D, wintypes.LPWSTR, ctypes.POINTER(D))


def conpty_available():
    return CreatePseudoConsole is not None


def _check(ok, what):
    if not ok:
        raise OSError(ctypes.get_last_error(), f"{what} failed")


def process_name(pid):
    """The lower-case file name of a running process (e.g. "claude.exe"), or None if it is not running."""
    h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        code = D()
        if not GetExitCodeProcess(h, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return None
        buf, size = ctypes.create_unicode_buffer(1024), D(1024)
        if not QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        CloseHandle(h)


def command_line(program, args):
    """The command line that starts program; npm installs Claude as a .cmd script, which needs cmd.exe."""
    line = subprocess.list2cmdline([program, *args])
    if program.lower().endswith((".cmd", ".bat")):
        return f'"{os.environ.get("ComSpec") or "cmd.exe"}" /d /s /c "{line}"'
    return line


class PseudoConsole:
    """One program running in its own ConPTY: read() its screen output, write() keys, resize() it."""

    def __init__(self, cmdline, cwd, env, cols, rows):
        self.exit_code = None
        self.exited = threading.Event()
        self._lock = threading.Lock()
        self._hpc = None
        in_read, in_write, out_read, out_write = H(), H(), H(), H()
        _check(CreatePipe(ctypes.byref(in_read), ctypes.byref(in_write), None, 0), "CreatePipe")
        _check(CreatePipe(ctypes.byref(out_read), ctypes.byref(out_write), None, 0), "CreatePipe")
        hpc = H()
        hr = CreatePseudoConsole(COORD(cols, rows), in_read, out_write, 0, ctypes.byref(hpc))
        if hr != 0:
            for h in (in_read, in_write, out_read, out_write):
                CloseHandle(h)
            raise OSError(f"CreatePseudoConsole failed (0x{hr & 0xFFFFFFFF:08X})")
        self._hpc, self._in, self._out = hpc, in_write, out_read
        try:
            size = ctypes.c_size_t()
            InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            attrs = ctypes.create_string_buffer(size.value)
            _check(InitializeProcThreadAttributeList(attrs, 1, 0, ctypes.byref(size)), "InitializeProcThreadAttributeList")
            try:
                _check(UpdateProcThreadAttribute(attrs, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, hpc, ctypes.sizeof(H), None, None),
                       "UpdateProcThreadAttribute")
                si = STARTUPINFOEXW()
                si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
                # Orbit's server runs with its output sent to a log file; without this the program would
                # write there instead of to the pseudo console
                si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
                si.lpAttributeList = ctypes.cast(attrs, P)
                block = "".join(f"{k}={v}\0" for k, v in sorted(env.items(), key=lambda kv: kv[0].upper())) + "\0"
                env_buf = ctypes.create_unicode_buffer(block, len(block) + 1)
                pi = PROCESS_INFORMATION()
                _check(CreateProcessW(None, ctypes.create_unicode_buffer(cmdline), None, None, False,
                                      EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT, ctypes.cast(env_buf, P),
                                      cwd, ctypes.byref(si), ctypes.byref(pi)), "CreateProcessW")
            finally:
                DeleteProcThreadAttributeList(attrs)
        except Exception:
            self.close()
            for h in (in_read, out_write, in_write, out_read):
                CloseHandle(h)
            raise
        # The pseudo console holds its own copies; closing ours lets read() see the end when it closes
        CloseHandle(in_read)
        CloseHandle(out_write)
        CloseHandle(pi.hThread)
        self._proc, self.pid = pi.hProcess, pi.dwProcessId
        threading.Thread(target=self._wait, daemon=True).start()

    def _wait(self):
        WaitForSingleObject(self._proc, INFINITE)
        code = D()
        if GetExitCodeProcess(self._proc, ctypes.byref(code)):
            self.exit_code = code.value
        self.exited.set()
        self.close()  # ends read() once the remaining output has been read

    def read(self, size=65536):
        buf, got = ctypes.create_string_buffer(size), D()
        if not ReadFile(self._out, buf, size, ctypes.byref(got), None) or got.value == 0:
            return b""
        return buf.raw[: got.value]

    def write(self, data):
        done = D()
        WriteFile(self._in, data, len(data), ctypes.byref(done), None)

    def resize(self, cols, rows):
        with self._lock:
            if self._hpc:
                ResizePseudoConsole(self._hpc, COORD(cols, rows))

    def close(self):
        """Close the console. Programs in it are told their window closed, like SIGHUP."""
        with self._lock:
            hpc, self._hpc = self._hpc, None
        if hpc:
            ClosePseudoConsole(hpc)

    def kill(self):
        if self.exited.is_set():
            return
        threading.Thread(target=self._kill, daemon=True).start()

    def _kill(self):
        self.close()
        if not self.exited.wait(3):  # still running after its window closed
            TerminateProcess(self._proc, 1)

    def release(self):
        """Free the remaining handles once the program has ended and its output is read."""
        for h in (self._in, self._out, self._proc):
            CloseHandle(h)
        self._in = self._out = self._proc = None
