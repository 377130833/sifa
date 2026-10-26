import ctypes
import sys
from ctypes import wintypes

pid = int(sys.argv[1])
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
if not handle:
    raise ctypes.WinError(ctypes.get_last_error())

try:
    class PROCESS_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("Reserved1", wintypes.LPVOID),
            ("PebBaseAddress", wintypes.LPVOID),
            ("Reserved2", wintypes.LPVOID * 2),
            ("UniqueProcessId", ctypes.c_size_t),
            ("Reserved3", wintypes.LPVOID),
        ]

    def read_memory(address, size):
        buffer = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t()
        ok = kernel32.ReadProcessMemory(
            handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(read)
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return buffer.raw[: read.value]

    pbi = PROCESS_BASIC_INFORMATION()
    status = ntdll.NtQueryInformationProcess(
        handle, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), None
    )
    if status != 0:
        raise RuntimeError(f"NtQueryInformationProcess failed: {status:#x}")

    peb = pbi.PebBaseAddress
    process_parameters = int.from_bytes(read_memory(peb + 0x20, 8), "little")
    current_directory = read_memory(process_parameters + 0x38, 16)
    length = int.from_bytes(current_directory[2:4], "little")
    buffer_address = int.from_bytes(current_directory[8:16], "little")
    print(read_memory(buffer_address, length).decode("utf-16-le"))
finally:
    kernel32.CloseHandle(handle)
