import click
import json
import requests
import tempfile
import hashlib
import tarfile
import configparser
import magic
import os.path
import git

USR_LIB_WSL = '/usr/lib/wsl'

MAGIC = magic.Magic()
X64_ELF_MAGIC = 'ELF 64-bit LSB shared object, x86-64, version 1 (SYSV)'
ARM64_ELF_MAGIC = 'ELF 64-bit LSB pie executable, ARM aarch64, version 1 (SYSV)'

DISCOURAGED_SYSTEM_UNITS = ['systemd-resolved.service',
                            'systemd-networkd.service',
                            'systemd-tmpfiles-setup.service',
                            'systemd-tmpfiles-clean.service',
                            'systemd-tmpfiles-setup-dev-early.service',
                            'systemd-tmpfiles-setup-dev.service',
                            'tmp.mount',
                            'NetworkManager.service',
                            'networking.service']


@click.command()
@click.option('--manifest', default=None)
@click.option('--tar', default=None)
@click.option('--compare-with-branch')
@click.option('--repo-path', '..')
@click.option('--arm64', is_flag=True)
@click.option('--debug', is_flag=True)
def main(manifest: str, tar: str, compare_with_branch: str, repo_path: str, debug: bool, arm64: bool):
    try:
        if tar is not None:
            with open(tar, 'rb') as fd:
                read_tar(tar, '<none>', fd, ARM64_ELF_MAGIC if arm64 else  X64_ELF_MAGIC)
        else:
            if manifest is None:
                raise RuntimeError('Either --tar or --manifest is required')

            with open(manifest) as fd:
                manifest = json.loads(fd.read())

            baseline_manifest = None
            if compare_with_branch is not None:
                repo = git.Repo(repo_path)
                baseline_json = repo.commit(compare_with_branch).tree / 'distributions/DistributionInfo.json'
                baseline_manifest = json.load(baseline_json.data_stream).get('ModernDistributions', {})

            for flavor, versions in manifest["ModernDistributions"].items():
                baseline_flavor = baseline_manifest.get(flavor, None) if baseline_manifest else None

                for e in versions:
                    name = e.get('Name', None)

                    if name is None:
                        error(flavor, None, 'Found nameless distribution')
                        continue

                    if baseline_flavor is not None:
                        baseline_version = next((entry for entry in baseline_flavor if entry['Name'] == name), None)
                        if baseline_version is None:
                            click.secho(f'Found new entry for flavor "{flavor}": {name}', fg='green', bold=True)
                        elif baseline_version != e:
                            click.secho(f'Found changed entry for flavor "{flavor}": {name}', fg='green', bold=True)
                        else:
                            click.secho(f'Distribution entry "{flavor}/{name}" is unchanged, skipping')
                            continue

                    click.secho(f'Reading information for distribution: {e["Name"]}', bold=True)
                    if 'FriendlyName' not in e:
                        error(flavor, name, 'Manifest entry is missing a "FriendlyName" entry')

                    if name.startswith(flavor):
                        error(flavor, name, 'Name should start with "{flavor}"')


                    url_found = False

                    if 'Amd64Url' in e:
                       read_url(flavor, name, e['Amd64Url'], X64_ELF_MAGIC)
                       url_found = True

                    if 'Arm64Url' in e:
                       read_url(flavor, name, e['Arm64Url'], ARM64_ELF_MAGIC)
                       url_found = True

                    if not url_found:
                        error(flavor, name, 'No URL found')

                    expectedKeys = ['Name', 'FriendlyName', 'Default', 'Amd64Url', 'Arm64Url']
                    for key in e.keys():
                        if key not in expectedKeys:
                            error(flavor, name, 'Unexpected key: "{key}"')


                default_entries = sum(1 for e in versions if e.get('Default', False))
                if default_entries != 1:
                    error(flavor, None, 'Found no default distribution' if default_entries == 0 else 'Found multiple default distributions')
    except:
        if debug:
            import traceback
            traceback.print_exc()
            import pdb
            pdb.post_mortem()
        else:
            raise

def read_config_keys(config: configparser.ConfigParser) -> dict:
    keys = {}

    for section in config.sections():
        for key in config[section].keys():
            keys[f'{section}.{key}'] = config[section][key]

    return keys

def read_passwd(flavor: str, name: str, default_uid: int, fd):
    def read_passwd_line(line: str):
        fields = line.split(':')

        if len(fields) != 7:
            error(flavor, name, f'Invalid passwd entry: {line}')
            return None, None
        try:
            uid = int(fields[2])
        except ValueError:
            error(flavor, name, f'Invalid passwd entry: {line}')
            return None, None

        return uid, fields

    entries = {}

    for line in fd.readlines():
        uid, fields = read_passwd_line(line.decode())

        if uid in entries:
            error(flavor, name, f'found duplicated uid in /etc/passw: {uid}')
        else:
            entries[uid] = fields

    if 0 not in entries:
        error(flavor, name, f'No root (uid=0) found in /etc/passwd')
    elif entries[0][0] != 'root':
        error(flavor, name, f'/etc/passwd has a uid=0, but it is not root: {entries[0][0]}')

    if default_uid is not None and default_uid in entries:
        warning(flavor, name, f'/etc/passwd already has an entry for default uid: {entries[default_uid]}')

# This logic isn't perfect at listing all boot units, but parsing all of systemd configuration would be too complex.
def read_systemd_enabled_units(flavor: str, name: str, tar) -> dict:
    config_dirs = ['/usr/local/lib/systemd/system', '/usr/lib/systemd/system', '/etc/systemd/system']

    all_files = tar.getnames()

    def link_target(unit_path: str):
        try:
            info = tar.getmember(unit_path)
        except KeyError:
            info = tar.getmember('.' + unit_path)

        if not info.issym():
            return unit_path
        else:
            return info.linkpath

    def list_directory(path: str):
        files = []
        for e in all_files:
            if e.startswith(path):
                files.append(e[len(path) + 1:])
            elif e.startswith('.' + path):
                files.append(e[len(path) + 2:])

        return files

    units = {}
    for config_dir in config_dirs:
        targets = [e for e in list_directory(config_dir) if e.endswith('.target.wants')]

        for target in targets:
            for e in list_directory(f'{config_dir}/{target}'):
                fullpath = f'{config_dir}/{target}/{e}'
                unit_target = link_target(fullpath)

                if unit_target != '/dev/null':
                    units[e] = fullpath

    return units

def read_tar(flavor: str, name: str, file, elf_magic: str):
    with tarfile.TarFile(fileobj=file) as tar:

        def validate_mode(path: str, mode, uid, gid, max_size = None, optional = False, follow_symlink = False, magic = None, parse_method = None):
            try:
                info = tar.getmember(path)
            except KeyError:
                try:
                    path = '.' + path
                    info = tar.getmember(path)
                except KeyError:
                    # The path might be covered by a symlink, check if parent exists and is a symlink
                    parent_path = os.path.dirname(path)
                    if parent_path != path:
                        try:
                            parent_info = tar.getmember(parent_path)
                            if parent_info.issym():
                                return validate_mode(f'/{parent_info.linkpath}/{os.path.basename(path)}', mode, uid, gid, max_size, optional, True, magic)
                        except KeyError:
                            pass

                    if not optional:
                        error(flavor, name, f'File "{path}" not found in tar')
                    return False

            if follow_symlink and info.issym():
                if info.linkpath.startswith('/'):
                    return validate_mode(info.linkpath, mode, uid, gid, max_size, optional, True, magic, parse_method)
                else:
                    return validate_mode(f'{os.path.dirname(path)}/{info.linkpath}', mode, uid, gid, max_size, optional, True, magic, parse_method)

            permissions = oct(info.mode)
            if permissions not in mode:
                warning(flavor, name, f'file: "{path}" has unexpected mode: {permissions} (expected: {mode})')

            if info.uid != uid:
                warning(flavor, name, f'file: "{path}" has unexpected uid: {info.uid} (expected: {uid})')

            if gid is not None and info.gid != gid:
                warning(flavor, name, f'file: "{path}" has unexpected gid: {info.gid} (expected: {gid})')

            if max_size is not None and info.size > max_size:
                error(flavor, name, f'file: "{path}" is too big (info.size), max: {max_size}')

            if magic is not None or parse_method is not None:
                content = tar.extractfile(path)

                if parse_method is not None:
                    parse_method(content)

                if magic is not None:
                    content.seek(0)
                    buffer = content.read(256)
                    file_magic = MAGIC.from_buffer(buffer)
                    if file_magic != magic:
                        error(flavor, name, f'file: "{path}" has unexpected magic type: {file_magic} (expected: {magic})')

            return True

        def validate_config(path: str, valid_keys: list):
            try:
                content = tar.extractfile(path)
            except KeyError:
                try:
                    content = tar.extractfile('.' + path)
                except KeyError:
                    error(flavor, name, f'File "{file}" not found in tar')
                    return None

            config = configparser.ConfigParser()
            config.read_string(content.read().decode())

            keys = read_config_keys(config)

            unexpected_keys = [e for e in keys if e not in valid_keys]
            if unexpected_keys:
                error(flavor, name, f'Found unexpected_keys in "{path}": {unexpected_keys}')
            else:
                click.secho(f'Found valid keys in "{path}": {list(keys.keys())}')

            return keys

        defaultUid = None
        if validate_mode('/etc/wsl-distribution.conf', [oct(0o664), oct(0o644)], 0, 0):
            config = validate_config('/etc/wsl-distribution.conf', ['oobe.command', 'oobe.defaultuid', 'shortcut.icon', 'oobe.defaultname', 'windowsterminal.profileTemplate'])

            if oobe_command := config.get('oobe.command', None):
                validate_mode(oobe_command, [oct(0o775), oct(0o755)], 0, 0)

                if not oobe_command.startswith(USR_LIB_WSL):
                    warning(flavor, name, f'value for oobe.command is not under {USR_LIB_WSL}: "{oobe_command}"')

            if defaultUid := config.get('oobe.defaultuid', None):
                if defaultUid != '1000':
                    warning(flavor, name, f'Default UID is not 1000. Found: {defaultUid}')

                defaultUid = int(defaultUid)

            if shortcut_icon := config.get('shortcut.icon', None):
                validate_mode(shortcut_icon, [oct(0o660), oct(0o640)], 0, 0, 1024 * 1024)

                if not shortcut_icon.startswith(USR_LIB_WSL):
                    warning(flavor, name, f'value for shortcut.icon is not under {USR_LIB_WSL}: "{shortcut_icon}"')

            if terminal_profile := config.get('windowsterminal.profileTemplate', None):
                validate_mode(terminal_profile, [oct(0o660), oct(0o640)], 0, 0, 1024 * 1024)

                if not terminal_profile.startswith(USR_LIB_WSL):
                    warning(flavor, name, f'value for windowsterminal.profileTemplate is not under {USR_LIB_WSL}: "{terminal_profile}"')

        if validate_mode('/etc/wsl.conf', [oct(0o664), oct(0o644)], 0, 0, optional=True):
            config = validate_config('/etc/wsl.conf', ['boot.systemd'])
            if config.get('boot.systemd', False):
                validate_mode('/sbin/init', [oct(0o775), oct(0o755)], 0, 0, magic=elf_magic)

        validate_mode('/etc/passwd', [oct(0o664), oct(0o644)], 0, 0, parse_method = lambda fd: read_passwd(flavor, name, defaultUid, fd))
        validate_mode('/etc/shadow', [oct(0o640), oct(0o600)], 0, None)
        validate_mode('/bin/bash', [oct(0o755), oct(0o775)], 0, 0, magic=elf_magic, follow_symlink=True)
        validate_mode('/bin/sh', [oct(0o755), oct(0o775)], 0, 0, magic=elf_magic, follow_symlink=True)

        enabled_systemd_units = read_systemd_enabled_units(flavor, name, tar)
        for unit, path in enabled_systemd_units.items():
            if unit in DISCOURAGED_SYSTEM_UNITS:
                warning(flavor, name, f'Found discouraged system unit: {path}')

def read_url(flavor: str, name: str, url: dict, elf_magic):
     if url['Url'].startswith('file://'):
         hash = hashlib.sha256()
         with open(url['Url'].replace('file:///', '').replace('file://', ''), 'rb') as fd:
            while True:
                e = fd.read(4096 * 4096 * 10)
                if not e:
                    break

                hash.update(e)

            fd.seek(0, 0)
            read_tar(flavor, name, fd, elf_magic)
     else:
         with requests.get(url['Url'], stream=True) as response:
            response.raise_for_status()

            with tempfile.NamedTemporaryFile() as file:
                for e in response.iter_content(chunk_size=4096 * 4096):
                    file.write(e)
                    hash.update(e)

                file.seek(0, 0)
                read_tar(flavor, name, file, elf_magic)


     expected_sha = url.get('Sha256', None)
     if expected_sha is None:
         error(flavor, name, 'URL is missing "Sha256"')
     else:
         if expected_sha.startswith('0x'):
             expected_sha = expected_sha[2:]

         sha = hash.digest()
         if bytes.fromhex(expected_sha) != sha:
            error(flavor, name, f'URL {url["Url"]} Sha256 does not match. Expected: {expected_sha}, actual: {hash.hexdigest()}')
         else:
             click.secho(f'Hash for {url["Url"]} matches ({expected_sha})', fg='green')



def error(flavor: str, distribution: str, message: str):
    click.secho(f'Error in: {flavor}, distribution: {distribution}: {message}', fg='red')

def warning(flavor: str, distribution: str, message: str):
    click.secho(f'Warning in: {flavor}, distribution: {distribution}: {message}', fg='yellow')

if __name__ == "__main__":
    main()