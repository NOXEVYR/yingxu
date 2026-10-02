"""Read release literals without importing application code or executing a build."""
import ast
import ctypes
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET


class IdentityError(ValueError):
    pass


@dataclass(frozen=True)
class BuildIdentity:
    version: str
    build_revision: str
    source_sha256: str

    @property
    def assembly_version(self):
        return self.version + '.0'

    @property
    def informational_version(self):
        return self.version + '+' + self.build_revision

    def metadata(self):
        return dict(version=self.version, build_revision=self.build_revision,
                    assembly_version=self.assembly_version,
                    identity_source_sha256=self.source_sha256)


def read_identity(root):
    """Accept exactly one top-level literal assignment per identity field."""
    path = Path(root) / 'yingxu' / '__init__.py'
    raw = path.read_bytes()
    if len(raw) > 65536:
        raise IdentityError('Application identity source is too large.')
    try:
        tree = ast.parse(raw.decode('utf-8-sig'))
    except (SyntaxError, UnicodeError):
        raise IdentityError('Application identity source is invalid.') from None
    values = {}
    for name in ('__version__', '__build__'):
        assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                       and len(node.targets) == 1
                       and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name]
        writes = [node for node in ast.walk(tree) if isinstance(node, ast.Name)
                  and isinstance(node.ctx, ast.Store) and node.id == name]
        if len(assignments) != 1 or len(writes) != 1:
            raise IdentityError('Application identity must be a unique literal.')
        value = assignments[0].value
        if not isinstance(value, ast.Constant) or type(value.value) is not str:
            raise IdentityError('Application identity must be a unique literal.')
        values[name] = value.value
    version, build = values['__version__'], values['__build__']
    if not re.fullmatch(r'(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,4})', version):
        raise IdentityError('Application version must contain three decimal components.')
    if any(int(part) > 65534 for part in version.split('.')):
        raise IdentityError('Application version exceeds the native assembly range.')
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}\.(0|[1-9][0-9]{0,8})', build):
        raise IdentityError('Application build revision is invalid.')
    return BuildIdentity(version, build, hashlib.sha256(raw).hexdigest())


def generated_native_source(identity):
    """All compiler branches receive this source, including Core-only fixtures."""
    return ('// Generated from yingxu/__init__.py; do not maintain version literals here.\n'
            'using System.Reflection;\n'
            '[assembly: AssemblyVersion("' + identity.assembly_version + '")]\n'
            '[assembly: AssemblyFileVersion("' + identity.assembly_version + '")]\n'
            '[assembly: AssemblyInformationalVersion("' + identity.informational_version + '")]\n'
            'namespace YingXu.Desktop { internal static class BuildIdentity {\n'
            'internal const string Version = "' + identity.version + '";\n'
            'internal const string BuildRevision = "' + identity.build_revision + '";\n'
            '} }\n').encode('utf-8')


def generated_native_manifest(template, identity):
    marker = '@YINGXU_ASSEMBLY_VERSION@'
    raw = Path(template).read_bytes()
    try:
        element = ET.fromstring(raw)
        own = element.find('{urn:schemas-microsoft-com:asm.v1}assemblyIdentity')
    except ET.ParseError:
        raise IdentityError('Native manifest template is invalid.') from None
    if own is None or own.get('version') != marker or raw.count(marker.encode()) != 1:
        raise IdentityError('Native manifest must use the application identity placeholder.')
    return raw.replace(marker.encode(), identity.assembly_version.encode('ascii'))


def require_manifest_identity(manifest, identity):
    if (not isinstance(manifest, dict) or manifest.get('version') != identity.version
            or manifest.get('build_revision') != identity.build_revision):
        raise IdentityError('Release manifest differs from the application identity.')


def native_metadata(path):
    """Inspect PE version resources through Windows API, without loading the EXE.

    Windows-package creation/verification fails closed on other platforms.
    Source identity parsing/generation remains portable and dependency-free.
    """
    if sys.platform != 'win32':
        raise IdentityError('Windows native identity inspection requires Windows.')
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise IdentityError('Native executable is missing or unsafe.')
    before = path.stat()
    version = ctypes.WinDLL('version', use_last_error=True)
    u32 = ctypes.c_uint32
    version.GetFileVersionInfoSizeW.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(u32))
    version.GetFileVersionInfoSizeW.restype = u32
    version.GetFileVersionInfoW.argtypes = (ctypes.c_wchar_p, u32, u32, ctypes.c_void_p)
    version.GetFileVersionInfoW.restype = ctypes.c_int
    version.VerQueryValueW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p,
                                      ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(u32))
    version.VerQueryValueW.restype = ctypes.c_int
    handle = u32()
    size = version.GetFileVersionInfoSizeW(str(path), ctypes.byref(handle))
    if not 52 <= size <= 1024*1024:
        raise IdentityError('Native executable version resource is invalid.')
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise IdentityError('Native executable version resource is unavailable.')
    def query(key):
        pointer, count = ctypes.c_void_p(), u32()
        if not version.VerQueryValueW(buffer, key, ctypes.byref(pointer), ctypes.byref(count)) or not pointer.value:
            raise IdentityError('Native executable identity field is missing.')
        return pointer, count.value
    pointer, count = query('\\')
    if count < 52:
        raise IdentityError('Native executable fixed version is invalid.')
    fixed = ctypes.cast(pointer, ctypes.POINTER(u32*13)).contents
    if fixed[0] != 0xfeef04bd:
        raise IdentityError('Native executable fixed version is invalid.')
    fixed_version = '.'.join(map(str, (fixed[2]>>16, fixed[2]&65535, fixed[3]>>16, fixed[3]&65535)))
    pointer, count = query('\\VarFileInfo\\Translation')
    if not 4 <= count <= 64 or count % 4:
        raise IdentityError('Native executable language table is invalid.')
    translations = ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint16*(count//2))).contents
    observed = []
    for index in range(0,len(translations),2):
        prefix = '\\StringFileInfo\\%04x%04x\\' % (translations[index], translations[index+1])
        values = {}
        for name in ('FileVersion','ProductVersion'):
            pointer, length = query(prefix+name)
            if not 1 <= length <= 128:
                raise IdentityError('Native executable identity text is invalid.')
            values[name] = ctypes.wstring_at(pointer, length).rstrip('\0')
        if values['FileVersion'] != fixed_version:
            raise IdentityError('Native executable fixed and string versions differ.')
        observed.append(values)
    after = path.stat()
    if ((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)
            != (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)
            or any(value != observed[0] for value in observed[1:])):
        raise IdentityError('Native executable identity changed during inspection.')
    return dict(file_version=observed[0]['FileVersion'], product_version=observed[0]['ProductVersion'])


def require_native_identity(path, identity):
    metadata = native_metadata(path)
    if (metadata['file_version'] != identity.assembly_version
            or metadata['product_version'] != identity.informational_version):
        raise IdentityError('Native executable differs from the application identity.')
    return metadata
