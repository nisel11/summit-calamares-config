#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import libcalamares
import os
import subprocess
import shutil
import gettext

import libcalamares.utils

_ = gettext.translation(
    "calamares-python",
    localedir=libcalamares.utils.gettext_path(),
    languages=libcalamares.utils.gettext_languages(),
    fallback=True,
).gettext

systemd_boot_template = """title Manjaro Summit
linux /arkdep/%target%/vmlinuz
initrd /amd-ucode.img
initrd /intel-ucode.img
initrd /arkdep/%target%/initramfs-linux.img
options root="UUID={uuid}" rootflags=subvol=/arkdep/deployments/%target%/rootfs lsm=landlock,lockdown,yama,integrity,apparmor,bpf quiet splash loglevel=3 systemd.show_status=auto rd.udev.log_level=3 rw
"""

luks_systemd_boot_template = """title Manjaro Summit
linux /arkdep/%target%/vmlinuz
initrd /amd-ucode.img
initrd /intel-ucode.img
initrd /arkdep/%target%/initramfs-linux.img
options rd.luks.name={uuid}=manjaro_root root=/dev/mapper/manjaro_root rootflags=subvol=/arkdep/deployments/%target%/rootfs lsm=landlock,lockdown,yama,integrity,apparmor,bpf quiet splash loglevel=3 systemd.show_status=auto rd.udev.log_level=3 rw
"""

fstab = """UUID="{root_uuid}"	/home			btrfs	rw,relatime,ssd,discard=async,space_cache=v2,subvol=arkdep/shared/home,compress=zstd			0 1
UUID="{root_uuid}"	/root			btrfs	rw,relatime,ssd,discard=async,space_cache=v2,subvol=arkdep/shared/root,compress=zstd			0 1
UUID="{root_uuid}"	/arkdep			btrfs	rw,relatime,ssd,discard=async,space_cache=v2,subvol=arkdep,compress=zstd				0 1
UUID="{root_uuid}"	/var/lib/flatpak	btrfs	rw,relatime,ssd,discard=async,space_cache=v2,subvol=arkdep/shared/flatpak,compress=zstd			0 1
UUID="{esp_uuid}"      	/boot     		vfat	rw,relatime,fmask=0022,dmask=0022,codepage=437,iocharset=ascii,shortname=mixed,utf8,errors=remount-ro	0 2
"""

group_file = """root:x:0:root
wheel:x:998:
"""

loader_conf = """timeout 5
console-mode max
editor yes
auto-entries yes
auto-firmware yes
"""

def pretty_name():
    return _("Install system.")

def get_uuid(mountpoint, notluks=False):
    partitions = libcalamares.globalstorage.value("partitions")
    for partition in partitions:
        if partition.get("mountPoint") == mountpoint:
            if partition.get("luksMapperName") and not notluks:
                mapper = f"/dev/mapper/{partition['luksMapperName']}"
                return subprocess.check_output(
                    ["blkid", "-s", "UUID", "-o", "value", mapper]
                ).decode().strip()
            return partition.get("uuid", "")
    return ""

def copy_systemd_boot(rootmountpoint):
    # Create required directories and copy systemd-boot EFI binary
    dirs = [
        os.path.join(rootmountpoint, "boot", "EFI", "BOOT"),
        os.path.join(rootmountpoint, "boot", "EFI", "systemd"),
        os.path.join(rootmountpoint, "boot", "loader", "entries")
    ]
    for d in dirs:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as e:
            libcalamares.utils.error("Failed to create directory {}: {}".format(d, e))
    
    src_efi = "/usr/lib/systemd/boot/efi/systemd-bootx64.efi"
    dst_systemd = os.path.join(rootmountpoint, "boot", "EFI", "systemd", "systemd-bootx64.efi")
    dst_boot = os.path.join(rootmountpoint, "boot", "EFI", "BOOT", "BOOTx64.EFI")
    try:
        shutil.copy(src_efi, dst_systemd)
        shutil.copy(src_efi, dst_boot)
    except Exception as e:
        libcalamares.utils.error("Error copying systemd-boot: {}".format(e))

# Write loader configuration to loader.conf
def create_loader_conf(rootmountpoint):
    loader_conf_path = os.path.join(rootmountpoint, "boot", "loader", "loader.conf")
    try:
        with open(loader_conf_path, "w") as f:
            f.write(loader_conf)
    except Exception as e:
        libcalamares.utils.error("Failed to create loader.conf: {}".format(e))
        return (_("Job Error"), _("Failed to create loader.conf"))

# Run arkdep init
def initialize_arkdep(env):
    try:
        subprocess.check_call(["arkdep", "init"], env=env)
    except subprocess.CalledProcessError as e:
        libcalamares.utils.error(_("Failed to init arkdep: {}").format(e))
        return (_("Initialization Error"), _("Failed to init arkdep"))
    return None

# Create fstab with UUID of partitions
def configure_fstab(rootmountpoint, rootuuid, espuuid):
    target_dir = os.path.join(rootmountpoint, "arkdep", "overlay", "etc")
    try:
        os.makedirs(target_dir, exist_ok=True)
    except Exception as e:
        libcalamares.utils.error("Failed to create /arkdep/overlay/etc dir: {}".format(e))
        return (_("Job Error"), _("Failed to create /arkdep/overlay/etc dir"))
    target_file = os.path.join(target_dir, "fstab")
    try:
        with open(target_file, "w") as f:
            f.write(fstab.format(root_uuid=rootuuid, esp_uuid=espuuid))
    except Exception as e:
        libcalamares.utils.error("Failed to write fstab: {}".format(e))
        return (_("Job Error"), _("Failed to write fstab"))

def configure_group_file(rootmountpoint):
    target_dir = os.path.join(rootmountpoint, "arkdep", "overlay", "etc")
    target_file = os.path.join(target_dir, "group")
    try:
        with open(target_file, "w") as f:
            f.write(group_file)
    except Exception as e:
        libcalamares.utils.error("Failed to write /etc/group: {}".format(e))
        return (_("Job Error"), _("Failed to write /etc/group"))

def configure_systemd_boot_templates(uuid, rootmountpoint):
    target_dir = os.path.join(rootmountpoint, "arkdep", "templates")
    target_file = os.path.join(target_dir, "systemd-boot")
    if "mapper" in uuid:
        template = luks_systemd_boot_template.format(uuid=uuid)
    else:
        template = systemd_boot_template.format(uuid=uuid)

    try:
        with open(target_file, "w") as f:
            f.write(template)
    except Exception as e:
        libcalamares.utils.error(_("Failed to write systemd-boot template: {}").format(e))
        return (_("Job Error"), _("Failed to write systemd-boot template"))

# Run arkdep deploy
def deploy_arkdep(env):
    try:
        subprocess.check_call(["arkdep", "deploy"], env=env)
    except subprocess.CalledProcessError as e:
        libcalamares.utils.error(_("Failed to deploy image with arkdep: {}").format(e))
        return (_("Deployment Error"), _("Failed to deploy image with arkdep"))
    return None

def find_deployments_dir(rootmountpoint):
    deployments_dir = os.path.join(rootmountpoint, "arkdep", "deployments")
    deployment_dirs = [d for d in os.listdir(deployments_dir)
                       if os.path.isdir(os.path.join(deployments_dir, d))]
    if not deployment_dirs:
        libcalamares.utils.error("No deployment found in {}".format(deployments_dir))
        return (_("Deployment Error"), _("No deployment found in deployments folder"))
    if len(deployment_dirs) > 1:
        libcalamares.utils.warning("Multiple deployments found in {}. Using the first one.".format(deployments_dir))
    return os.path.join(deployments_dir, deployment_dirs[0])

def remount(deployment_dir, rootmountpoint, root_device, boot_device):
    bootDir = os.path.join(rootmountpoint, "boot")
    homeDir = os.path.join(rootmountpoint, "home")
    deploymentSubvol = "/arkdep" + deployment_dir.split("/arkdep", 1)[1] + "/rootfs"
    try:
        subprocess.check_call(["umount", bootDir])
        libcalamares.utils.debug("Unmounted boot")
        subprocess.check_call(["umount", rootmountpoint])
        libcalamares.utils.debug("Unmounted root")
        subprocess.check_call(["mount", "-o", "subvol=" + deploymentSubvol, root_device, rootmountpoint])
        libcalamares.utils.debug("Mounted deployment")
        subprocess.check_call(["mount", "-o", "subvol=/arkdep/shared/home", root_device, homeDir])
        libcalamares.utils.debug("Mounted home")
        subprocess.check_call(["mount", boot_device, bootDir])
        libcalamares.utils.debug("Mounted boot")
    except subprocess.CalledProcessError as e:
        libcalamares.utils.error(f"Failed to remount: {e}")
        return (_("Deployment Error"), _("Failed to remount"))
    return None

def run():
    rootmountpoint = libcalamares.globalstorage.value("rootMountPoint")
    partitions = libcalamares.globalstorage.value("partitions")

    arkdep_env = os.environ.copy()
    arkdep_env["ARKDEP_NO_BOOTCTL"] = "1"
    arkdep_env["ARKDEP_ROOT"] = rootmountpoint

    root_uuid = get_uuid("/")
    esp_uuid  = get_uuid("/boot")
    luks_uuid = None

    for partition in partitions:
        if partition.get("mountPoint") == "/":
            root_device = partition["device"]
            if "luksMapperName" in partition:
                root_device = os.path.join("/dev/mapper", partition["luksMapperName"])
                luks_uuid = get_uuid("/", True)
        if partition.get("mountPoint") == "/boot":
            boot_device = partition["device"]
    
    copy_systemd_boot(rootmountpoint)
    libcalamares.utils.debug("Systemd-boot EFI binaries copied")

    create_loader_conf(rootmountpoint)
    libcalamares.utils.debug("loader.conf created")

    initialize_arkdep(arkdep_env)
    libcalamares.utils.debug("Arkdep initialized")

    configure_fstab(rootmountpoint, root_uuid, esp_uuid)
    libcalamares.utils.debug("fstab configured")

    configure_group_file(rootmountpoint)
    libcalamares.utils.debug("group file configured")

    configure_systemd_boot_templates(luks_uuid or root_uuid, rootmountpoint)
    libcalamares.utils.debug("Systemd-boot template configured")

    deploy_arkdep(arkdep_env)
    libcalamares.utils.debug("Arkdep deploy is finished")

    deployment_dir = find_deployments_dir(rootmountpoint)
    remount(deployment_dir, rootmountpoint, root_device, boot_device)
    return None
