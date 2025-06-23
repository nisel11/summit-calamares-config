#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import libcalamares
import libcalamares.utils
import os
import subprocess
import shutil
import gettext
import re
import threading
import time

_ = gettext.translation(
    "calamares-python",
    localedir=libcalamares.utils.gettext_path(),
    languages=libcalamares.utils.gettext_languages(),
    fallback=True,
).gettext

TEMPLATES = {
    'systemd_boot': """title Manjaro Summit
linux /arkdep/%target%/vmlinuz
initrd /amd-ucode.img
initrd /intel-ucode.img
initrd /arkdep/%target%/initramfs-linux.img
options root="UUID={root_uuid}" rootflags=subvol=/arkdep/deployments/%target%/rootfs {home_option} lsm=landlock,lockdown,yama,integrity,apparmor,bpf quiet splash loglevel=3 systemd.show_status=auto rd.udev.log_level=3 rw
""",
    'luks_systemd_boot': """title Manjaro Summit
linux /arkdep/%target%/vmlinuz
initrd /amd-ucode.img
initrd /intel-ucode.img
initrd /arkdep/%target%/initramfs-linux.img
options rd.luks.name={root_uuid}=manjaro_root root=/dev/mapper/manjaro_root rootflags=subvol=/arkdep/deployments/%target%/rootfs {home_option} lsm=landlock,lockdown,yama,integrity,apparmor,bpf quiet splash loglevel=3 systemd.show_status=auto rd.udev.log_level=3 rw
""",
    'fstab': """UUID="{root_fs_uuid}"	/root			btrfs	rw,relatime,ssd,discard=async,space_cache=v2,subvol=arkdep/shared/root,compress=zstd			0 1
UUID="{root_fs_uuid}"	/arkdep			btrfs	rw,relatime,ssd,discard=async,space_cache=v2,subvol=arkdep,compress=zstd				0 1
UUID="{root_fs_uuid}"	/var/lib/flatpak	btrfs	rw,relatime,ssd,discard=async,space_cache=v2,subvol=arkdep/shared/flatpak,compress=zstd			0 1
UUID="{esp_uuid}"      	/boot     		vfat	rw,relatime,fmask=0022,dmask=0022,codepage=437,iocharset=ascii,shortname=mixed,utf8,errors=remount-ro	0 2
{home_fstab}
""",
    'home_fstab': """UUID="{home_fs_uuid}"	/home			{fs_type}	{mount_options}	0 1
""",
    'group_file': """root:x:0:root
wheel:x:998:
""",
    'loader_conf': """timeout 5
console-mode max
editor yes
auto-entries yes
auto-firmware yes
"""
}

def pretty_name():
    return _("Install system.")

class PartitionManager:
    def __init__(self):
        self.partitions = libcalamares.globalstorage.value("partitions")

    def get_partition(self, mountpoint):
        for p in self.partitions:
            if p.get("mountPoint") == mountpoint:
                return p
        return {}

    def get_uuid(self, mountpoint, filesystem=False):
        partition = self.get_partition(mountpoint)
        if not partition:
            return ""

        if partition.get("luksMapperName"):
            if filesystem:
                mapper = f"/dev/mapper/{partition['luksMapperName']}"
                if os.path.exists(mapper):
                    try:
                        return subprocess.check_output(["blkid", "-s", "UUID", "-o", "value", mapper]).decode().strip()
                    except subprocess.CalledProcessError:
                        pass
            else:
                return partition.get("luksUuid", "")

        return partition.get("uuid", "")

    def get_device(self, mountpoint):
        partition = self.get_partition(mountpoint)
        if not partition:
            return None

        device = partition.get("device")
        if partition.get("luksMapperName"):
            device = f"/dev/mapper/{partition['luksMapperName']}"

        return device

    def is_luks(self, mountpoint):
        return bool(self.get_partition(mountpoint).get("luksMapperName"))

    def get_filesystem(self, mountpoint):
        return self.get_partition(mountpoint).get("fs", "btrfs")

class FileManager:
    @staticmethod
    def write(path, content):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            return True
        except Exception as e:
            libcalamares.utils.error(f"Failed to write {path}: {e}")
            return False

    @staticmethod
    def run_cmd(cmd, env=None):
        try:
            subprocess.check_call(cmd, env=env)
            return True
        except subprocess.CalledProcessError as e:
            libcalamares.utils.error(f"Command failed: {e}")
            return False

    @staticmethod
    def run_cmd_with_logging(cmd, env=None, log_interval=1):
        last_output_line = ""

        def log_progress():
            nonlocal last_output_line
            counter = 0
            while process.poll() is None:
                counter += log_interval
                if last_output_line.strip():
                    libcalamares.utils.debug(f"[{counter}s] {last_output_line.strip()}")
                else:
                    libcalamares.utils.debug(f"[{counter}s] Running: {' '.join(cmd)}")
                time.sleep(log_interval)

        def read_output():
            nonlocal last_output_line
            for line in iter(process.stdout.readline, b''):
                line_str = line.decode().strip()
                if line_str:
                    last_output_line = line_str

        try:
            process = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=False)

            log_thread = threading.Thread(target=log_progress)
            log_thread.daemon = True
            log_thread.start()

            output_thread = threading.Thread(target=read_output)
            output_thread.daemon = True
            output_thread.start()

            process.wait()

            if process.returncode != 0:
                libcalamares.utils.error(f"Command failed with code {process.returncode}. Last output: {last_output_line}")
                return False

            libcalamares.utils.debug(f"Command completed successfully: {' '.join(cmd)}")
            return True

        except Exception as e:
            libcalamares.utils.error(f"Command failed: {e}")
            return False

class Arkdep:
    def __init__(self, rootmount):
        self.rootmount = rootmount
        self.pm = PartitionManager()
        self.fm = FileManager()

    def setup_systemd_boot(self):
        dirs = [
            os.path.join(self.rootmount, "boot", "EFI", "BOOT"),
            os.path.join(self.rootmount, "boot", "EFI", "systemd"),
            os.path.join(self.rootmount, "boot", "loader", "entries")
        ]
        for d in dirs:
            os.makedirs(d, exist_ok=True)

        src = "/usr/lib/systemd/boot/efi/systemd-bootx64.efi"
        destinations = [
            os.path.join(self.rootmount, "boot", "EFI", "systemd", "systemd-bootx64.efi"),
            os.path.join(self.rootmount, "boot", "EFI", "BOOT", "BOOTx64.EFI")
        ]

        for dst in destinations:
            try:
                shutil.copy(src, dst)
            except Exception as e:
                libcalamares.utils.error(f"Error copying systemd-boot: {e}")
                return False

        loader_conf = os.path.join(self.rootmount, "boot", "loader", "loader.conf")
        return self.fm.write(loader_conf, TEMPLATES['loader_conf'])

    def update_arkdep_config(self):
        config = libcalamares.job.configuration
        repo_url = config.get("repoUrl")
        repo_image = config.get("repoImage")

        if not repo_url and not repo_image:
            return True

        config_path = os.path.join(self.rootmount, "arkdep", "config")
        if not os.path.exists(config_path):
            return True

        try:
            with open(config_path, "r") as f:
                content = f.read()

            if repo_url:
                content = re.sub(r"repo_url\s*=\s*['\"][^'\"]*['\"]", f"repo_url='{repo_url}'", content)
            if repo_image:
                content = re.sub(r"repo_default_image\s*=\s*['\"][^'\"]*['\"]", f"repo_default_image='{repo_image}'", content)

            with open(config_path, "w") as f:
                f.write(content)
            return True
        except Exception as e:
            libcalamares.utils.error(f"Failed to update arkdep config: {e}")
            return False

    def configure_fstab(self):
        root_fs_uuid = self.pm.get_uuid("/", filesystem=True)
        esp_uuid = self.pm.get_uuid("/boot")

        home_partition = self.pm.get_partition("/home")
        if home_partition:
            fs_type = self.pm.get_filesystem("/home")
            mount_options = ("rw,relatime,ssd,discard=async,space_cache=v2,compress=zstd"
                           if fs_type == "btrfs" else "rw,relatime,errors=remount-ro")
            home_fstab = TEMPLATES['home_fstab'].format(
                home_fs_uuid=self.pm.get_uuid("/home", filesystem=True),
                fs_type=fs_type,
                mount_options=mount_options
            )
        else:
            home_fstab = TEMPLATES['home_fstab'].format(
                home_fs_uuid=root_fs_uuid,
                fs_type="btrfs",
                mount_options="rw,relatime,ssd,discard=async,space_cache=v2,subvol=arkdep/shared/home,compress=zstd"
            )

        fstab_content = TEMPLATES['fstab'].format(
            root_fs_uuid=root_fs_uuid,
            esp_uuid=esp_uuid,
            home_fstab=home_fstab
        )

        fstab_path = os.path.join(self.rootmount, "arkdep", "overlay", "etc", "fstab")
        return self.fm.write(fstab_path, fstab_content)

    def configure_boot_template(self):
        root_uuid = self.pm.get_uuid("/", filesystem=False)
        home_option = ""

        if self.pm.is_luks("/home"):
            home_luks_uuid = self.pm.get_uuid("/home", filesystem=False)
            if home_luks_uuid:
                home_option = f"rd.luks.name={home_luks_uuid}=manjaro_home"

        template_key = 'luks_systemd_boot' if self.pm.is_luks("/") else 'systemd_boot'
        template_content = TEMPLATES[template_key].format(
            root_uuid=root_uuid,
            home_option=home_option
        )

        template_path = os.path.join(self.rootmount, "arkdep", "templates", "systemd-boot")
        return self.fm.write(template_path, template_content)

    def remount_system(self):
        devices = {
            'root': self.pm.get_device("/"),
            'boot': self.pm.get_device("/boot"),
            'home': self.pm.get_device("/home")
        }

        if not devices['root'] or not devices['boot']:
            libcalamares.utils.error("Required devices not found")
            return False

        deployments_dir = os.path.join(self.rootmount, "arkdep", "deployments")
        deployment_dirs = [d for d in os.listdir(deployments_dir) if os.path.isdir(os.path.join(deployments_dir, d))]

        if not deployment_dirs:
            libcalamares.utils.error("No deployment found")
            return False

        deployment_subvol = f"/arkdep/deployments/{deployment_dirs[0]}/rootfs"

        boot_dir = os.path.join(self.rootmount, "boot")
        home_dir = os.path.join(self.rootmount, "home")

        commands = [
            ["umount", boot_dir],
            ["umount", home_dir] if os.path.ismount(home_dir) else None,
            ["umount", self.rootmount],
            ["mount", "-o", f"subvol={deployment_subvol}", devices['root'], self.rootmount],
            ["mount", devices['home'], home_dir] if devices['home'] else
            ["mount", "-o", "subvol=/arkdep/shared/home", devices['root'], home_dir],
            ["mount", devices['boot'], boot_dir]
        ]

        for cmd in filter(None, commands):
            if not self.fm.run_cmd(cmd):
                return False

        return True

    def install(self):
        arkdep_env = os.environ.copy()
        arkdep_env.update({"ARKDEP_NO_BOOTCTL": "1", "ARKDEP_ROOT": self.rootmount})

        steps = [
            ("Setup systemd-boot", self.setup_systemd_boot),
            ("Initialize arkdep", lambda: self.fm.run_cmd(["arkdep", "init"], arkdep_env)),
            ("Update arkdep config", self.update_arkdep_config),
            ("Configure fstab", self.configure_fstab),
            ("Write group file", lambda: self.fm.write(
                os.path.join(self.rootmount, "arkdep", "overlay", "etc", "group"),
                TEMPLATES['group_file']
            )),
            ("Configure boot template", self.configure_boot_template),
            ("Deploy system", lambda: self.fm.run_cmd_with_logging(["arkdep", "deploy"], arkdep_env)),
            ("Remount system", self.remount_system)
        ]

        for description, func in steps:
            if not func():
                return (_("Installation Error"), _(f"Failed: {description}"))

        return None

def run():
    rootmount = libcalamares.globalstorage.value("rootMountPoint")
    installer = Arkdep(rootmount)
    return installer.install()
