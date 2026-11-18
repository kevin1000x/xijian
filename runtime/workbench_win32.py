"""Small Win32 launcher for the synthetic workbench AppContainer policy."""
import ctypes as c
from ctypes import wintypes as w
import msvcrt
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import uuid
import weakref


P = c.c_void_p
SIZE = c.c_size_t
H = w.HANDLE
DWORD = w.DWORD
PTR = c.POINTER
ref = c.byref
kernel = c.WinDLL('kernel32', use_last_error=True)
advapi = c.WinDLL('advapi32', use_last_error=True)
userenv = c.WinDLL('userenv', use_last_error=True)


def api(dll, name, result, *arguments):
    function = getattr(dll, name)
    function.restype, function.argtypes = result, list(arguments)
    return function


def check(result):
    if not result:
        raise c.WinError(c.get_last_error())
    return result


class Startup(c.Structure):
    _fields_ = [('cb', DWORD), ('reserved', P), ('desktop', P), ('title', P),
                ('x', DWORD), ('y', DWORD), ('xSize', DWORD), ('ySize', DWORD),
                ('xChars', DWORD), ('yChars', DWORD), ('fill', DWORD), ('flags', DWORD),
                ('show', w.WORD), ('size', w.WORD), ('reserved2', P),
                ('stdin', H), ('stdout', H), ('stderr', H)]


class ExtendedStartup(c.Structure):
    _fields_ = [('startup', Startup), ('attributes', P)]


class ProcessInfo(c.Structure):
    _fields_ = [('process', H), ('thread', H), ('pid', DWORD), ('tid', DWORD)]


class SecurityAttributes(c.Structure):
    _fields_ = [('length',DWORD),('descriptor',P),('inherit',w.BOOL)]


class Capabilities(c.Structure):
    _fields_ = [('sid', P), ('groups', P), ('count', DWORD), ('reserved', DWORD)]


create_profile = api(userenv, 'CreateAppContainerProfile', w.LONG, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, P, DWORD, PTR(P))
delete_profile = api(userenv, 'DeleteAppContainerProfile', w.LONG, w.LPCWSTR)
free_sid = api(advapi, 'FreeSid', P, P)
string_sid = api(advapi, 'ConvertSidToStringSidW', w.BOOL, P, PTR(P))
local_free = api(kernel, 'LocalFree', P, P)
init_list = api(kernel, 'InitializeProcThreadAttributeList', w.BOOL, P, DWORD, DWORD, PTR(SIZE))
set_attribute = api(kernel, 'UpdateProcThreadAttribute', w.BOOL, P, DWORD, SIZE, P, SIZE, P, P)
delete_list = api(kernel, 'DeleteProcThreadAttributeList', None, P)
create_process = api(kernel, 'CreateProcessW', w.BOOL, w.LPCWSTR, w.LPWSTR, P, P, w.BOOL, DWORD, P, w.LPCWSTR, P, PTR(ProcessInfo))
close = api(kernel, 'CloseHandle', w.BOOL, H)
open_token = api(advapi, 'OpenProcessToken', w.BOOL, H, DWORD, PTR(H))
get_token = api(advapi, 'GetTokenInformation', w.BOOL, H, c.c_int, P, DWORD, PTR(DWORD))
equal_sid = api(advapi, 'EqualSid', w.BOOL, P, P)
duplicate_token = api(advapi, 'DuplicateToken', w.BOOL, H, c.c_int, PTR(H))
make_sd = api(advapi, 'ConvertStringSecurityDescriptorToSecurityDescriptorW', w.BOOL, w.LPCWSTR, DWORD, PTR(P), P)
create_directory = api(kernel, 'CreateDirectoryW', w.BOOL, w.LPCWSTR, PTR(SecurityAttributes))
current_process = api(kernel,'GetCurrentProcess',H)
access_check = api(advapi, 'AccessCheck', w.BOOL, P, H, DWORD, P, P, PTR(DWORD), PTR(DWORD), PTR(w.BOOL))
resume = api(kernel, 'ResumeThread', DWORD, H)
terminate = api(kernel, 'TerminateProcess', w.BOOL, H, w.UINT)
wait_for = api(kernel, 'WaitForSingleObject', DWORD, H, DWORD)
exit_code = api(kernel, 'GetExitCodeProcess', w.BOOL, H, PTR(DWORD))
in_job = api(kernel, 'IsProcessInJob', w.BOOL, H, H, PTR(w.BOOL))
peek = api(kernel, 'PeekNamedPipe', w.BOOL, H, P, DWORD, P, PTR(DWORD), P)


class Profile:
    def __init__(self):
        self.name = 'Xijian.Synthetic.' + uuid.uuid4().hex
        self.sid = P()
        self.text = None

    def __enter__(self):
        hr = create_profile(self.name, self.name, 'Temporary synthetic analysis', None, 0, ref(self.sid))
        if hr:
            raise OSError(hr, 'CreateAppContainerProfile')
        try:
            out = P()
            check(string_sid(self.sid, ref(out)))
            try:
                self.text = c.wstring_at(out)
            finally:
                local_free(out)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        free_sid(self.sid)
        hr = delete_profile(self.name)
        if hr:
            raise OSError(hr, 'DeleteAppContainerProfile')


def grant(path, sid, rights, low=False):
    exe = Path(os.environ['SystemRoot']) / 'System32' / 'icacls.exe'
    # All targets are newly created, broker-owned files/directories.
    for args in (['/grant', '*' + sid + ':' + rights], ['/setintegritylevel', 'L'] if low else []):
        if args:
            subprocess.run([str(exe), str(path)] + args, check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=0x08000000)


def token_info(token, info_class):
    length = DWORD()
    get_token(token, info_class, None, 0, ref(length))
    if not 0 < length.value <= 65536:
        raise OSError(c.get_last_error(), 'token query length %d' % info_class)
    buffer = c.create_string_buffer(length.value)
    check(get_token(token, info_class, buffer, length, ref(length)))
    return buffer


class PrivateDirectory:
    """Create a fresh, protected directory even under a shared NTFS TEMP."""
    def __init__(self, prefix):
        token, sid_text, descriptor = H(), P(), P()
        self.name = str(Path(tempfile.gettempdir()) / (prefix + uuid.uuid4().hex))
        check(open_token(current_process(),8,ref(token)))
        try:
            user = token_info(token,1)
            check(string_sid(P.from_buffer(user).value,ref(sid_text)))
            sid = c.wstring_at(sid_text)
            dacl = 'D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;' + sid + ')'
            check(make_sd(dacl,1,ref(descriptor),None))
            attributes = SecurityAttributes(c.sizeof(SecurityAttributes),descriptor,False)
            check(create_directory(self.name,ref(attributes)))
        finally:
            if descriptor: local_free(descriptor)
            if sid_text: local_free(sid_text)
            close(token)
        self.cleanup = weakref.finalize(self, shutil.rmtree, self.name)

    def __enter__(self):
        return self.name

    def __exit__(self, *_):
        self.cleanup()


def verify_package_access(token):
    # Both package groups are available in this explicitly standard AppContainer.
    descriptor, duplicate = P(), H()
    check(make_sd('O:SYG:SYD:(A;;0x3;;;WD)(A;;0x1;;;AC)(A;;0x2;;;S-1-15-2-2)',1,ref(descriptor),None))
    try:
        check(duplicate_token(token,2,ref(duplicate)))
        mapping = (DWORD * 4)(1,2,0,3)
        for bit, expected in ((1,True),(2,True)):
            privileges = c.create_string_buffer(1024)
            size, granted, allowed = DWORD(len(privileges)), DWORD(), w.BOOL()
            check(access_check(descriptor,duplicate,bit,mapping,privileges,ref(size),ref(granted),ref(allowed)))
            if bool(allowed.value) != expected or granted.value != (bit if expected else 0):
                raise PermissionError('standard package access mismatch')
    finally:
        if duplicate: close(duplicate)
        local_free(descriptor)


def verify_token(process, expected_sid):
    token = H()
    check(open_token(process, 0x000a, ref(token)))
    try:
        def scalar(info):
            return DWORD.from_buffer(token_info(token, info)).value
        sid_data = token_info(token, 31)
        sid = P.from_buffer(sid_data).value
        if scalar(29) != 1 or scalar(30) != 0 or not sid or not equal_sid(sid, expected_sid):
            raise PermissionError('AppContainer token policy mismatch')
        verify_package_access(token)
    finally:
        close(token)


def start(profile, command, cwd, environment, stdin, stdout, job):
    child_in, child_out = [msvcrt.get_osfhandle(fd) for fd in (stdin, stdout)]
    handles = (H * 2)(child_in, child_out)
    capabilities = Capabilities(profile.sid, None, 0, 0)
    attributes = [(0x20009, ref(capabilities), c.sizeof(capabilities)),
                  (0x20002, handles, c.sizeof(handles))]
    length = SIZE()
    init_list(None, len(attributes), 0, ref(length))
    storage = c.create_string_buffer(length.value)
    check(init_list(storage, len(attributes), 0, ref(length)))
    process = ProcessInfo()
    try:
        for attribute, value, size in attributes:
            check(set_attribute(storage, 0, attribute, value, size, None, None))
        startup = ExtendedStartup()
        startup.startup.cb = c.sizeof(startup)
        startup.startup.flags = 0x100
        startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = child_in, child_out, child_out
        startup.attributes = c.cast(storage, P)
        env = c.create_unicode_buffer('\0'.join(k + '=' + v for k,v in sorted(environment.items())) + '\0\0')
        cmd = c.create_unicode_buffer(subprocess.list2cmdline([str(v) for v in command]))
        os.set_handle_inheritable(child_in, True)
        os.set_handle_inheritable(child_out, True)
        try:
            check(create_process(str(command[0]), cmd, None, None, True, 0x08080404,
                                 env, str(cwd), ref(startup), ref(process)))
        finally:
            os.set_handle_inheritable(child_in, False)
            os.set_handle_inheritable(child_out, False)
        verify_token(process.process, profile.sid)
        check(job.kernel.AssignProcessToJobObject(job.handle, process.process))
        member = w.BOOL()
        check(in_job(process.process, job.handle, ref(member)))
        if not member.value:
            raise PermissionError('Job membership')
        if resume(process.thread) == 0xffffffff:
            raise c.WinError(c.get_last_error())
        return process
    except BaseException:
        if process.process:
            terminate(process.process, 1)
            wait_for(process.process, 3000)
            close(process.process)
        raise
    finally:
        if process.thread:
            close(process.thread)
        delete_list(storage)


def drain(fd, budget):
    available = DWORD()
    if not peek(msvcrt.get_osfhandle(fd), None, 0, None, ref(available), None):
        if c.get_last_error() in (109, 232):
            return 0
        raise c.WinError(c.get_last_error())
    if available.value and budget > 0:
        data = os.read(fd,min(available.value,budget))
        return len(data)
    return 0
