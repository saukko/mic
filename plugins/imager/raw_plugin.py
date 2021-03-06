#!/usr/bin/python -tt
#
# Copyright (c) 2011 Intel, Inc.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation; version 2 of the License
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License
# for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program; if not, write to the Free Software Foundation, Inc., 59
# Temple Place - Suite 330, Boston, MA 02111-1307, USA.

import os
import shutil
import re
import tempfile

from mic import chroot, msger, rt_util
from mic.utils import misc, fs_related, errors, runner
from mic.conf import configmgr
from mic.plugin import pluginmgr
from mic.utils.partitionedfs import PartitionedMount

import mic.imager.raw as raw

from mic.pluginbase import ImagerPlugin
class RawPlugin(ImagerPlugin):
    name = 'raw'

    @classmethod
    def do_create(self, subcmd, opts, *args):
        """${cmd_name}: create raw image

        Usage:
            ${name} ${cmd_name} <ksfile> [OPTS]

        ${cmd_option_list}
        """

        if not args:
            raise errors.Usage("need one argument as the path of ks file")

        if len(args) != 1:
            raise errors.Usage("Extra arguments given")

        creatoropts = configmgr.create
        ksconf = args[0]

        if not os.path.exists(ksconf):
            raise errors.CreatorError("Can't find the file: %s" % ksconf)

        recording_pkgs = []
        if len(creatoropts['record_pkgs']) > 0:
            recording_pkgs = creatoropts['record_pkgs']

        if creatoropts['release'] is not None:
            if 'name' not in recording_pkgs:
                recording_pkgs.append('name')
            ksconf = misc.save_ksconf_file(ksconf, creatoropts['release'])

        configmgr._ksconf = ksconf
    
        # Called After setting the configmgr._ksconf as the creatoropts['name'] is reset there.
        if creatoropts['release'] is not None:
            creatoropts['outdir'] = "%s/%s/images/%s/" % (creatoropts['outdir'], creatoropts['release'], creatoropts['name'])

        # try to find the pkgmgr
        pkgmgr = None
        for (key, pcls) in pluginmgr.get_plugins('backend').iteritems():
            if key == creatoropts['pkgmgr']:
                pkgmgr = pcls
                break

        if not pkgmgr:
            pkgmgrs = pluginmgr.get_plugins('backend').keys()
            raise errors.CreatorError("Can't find package manager: %s (availables: %s)" % (creatoropts['pkgmgr'], ', '.join(pkgmgrs)))

        if creatoropts['runtime']:
            rt_util.runmic_in_runtime(creatoropts['runtime'], creatoropts, ksconf, None)

        creator = raw.RawImageCreator(creatoropts, pkgmgr)

        if len(recording_pkgs) > 0:
            creator._recording_pkgs = recording_pkgs

        if creatoropts['release'] is None:
            for item in creator.get_diskinfo():
                imagefile = "%s-%s.raw" % (os.path.join(creator.destdir, creator.name), item['name'])
                if os.path.exists(imagefile):
                    if msger.ask('The target image: %s already exists, cleanup and continue?' % imagefile):
                       os.unlink(imagefile)
                    else:
                       raise errors.Abort('Canceled')

        try:
            creator.check_depend_tools()
            creator.mount(None, creatoropts["cachedir"])
            creator.install()
            creator.configure(creatoropts["repomd"])
            creator.copy_kernel()
            creator.unmount()
            creator.package(creatoropts["outdir"])
            if creatoropts['release'] is not None:
                creator.release_output(ksconf, creatoropts['outdir'], creatoropts['release'])
            creator.print_outimage_info()

        except errors.CreatorError:
            raise
        finally:
            creator.cleanup()

        msger.info("Finished.")
        return 0

    @classmethod
    def do_chroot(cls, target):
        img = target
        imgsize = misc.get_file_size(img) * 1024L * 1024L
        partedcmd = fs_related.find_binary_path("parted")
        disk = fs_related.SparseLoopbackDisk(img, imgsize)
        imgmnt = misc.mkdtemp()
        imgloop = PartitionedMount({'/dev/sdb':disk}, imgmnt, skipformat = True)
        img_fstype = "ext3"

        # Check the partitions from raw disk.
        root_mounted = False
        partition_mounts = 0
        for line in runner.outs([partedcmd,"-s",img,"unit","B","print"]).splitlines():
            line = line.strip()

            # Lines that start with number are the partitions,
            # because parted can be translated we can't refer to any text lines.
            if not line or not line[0].isdigit():
                continue

            # Some vars have extra , as list seperator.
            line = line.replace(",","")

            # Example of parted output lines that are handled:
            # Number  Start        End          Size         Type     File system     Flags
            #  1      512B         3400000511B  3400000000B  primary
            #  2      3400531968B  3656384511B  255852544B   primary  linux-swap(v1)
            #  3      3656384512B  3720347647B  63963136B    primary  fat16           boot, lba

            partition_info = re.split("\s+",line)

            size = partition_info[3].split("B")[0]

            if len(partition_info) < 6 or partition_info[5] in ["boot"]:
                # No filesystem can be found from partition line. Assuming
                # btrfs, because that is the only MeeGo fs that parted does
                # not recognize properly.
                # TODO: Can we make better assumption?
                fstype = "btrfs"
            elif partition_info[5] in ["ext2","ext3","ext4","btrfs"]:
                fstype = partition_info[5]
            elif partition_info[5] in ["fat16","fat32"]:
                fstype = "vfat"
            elif "swap" in partition_info[5]:
                fstype = "swap"
            else:
                raise errors.CreatorError("Could not recognize partition fs type '%s'." % partition_info[5])

            if not root_mounted and fstype in ["ext2","ext3","ext4","btrfs"]:
                # TODO: Check that this is actually the valid root partition from /etc/fstab
                mountpoint = "/"
                root_mounted = True
            elif fstype == "swap":
                mountpoint = "swap"
            else:
                # TODO: Assing better mount points for the rest of the partitions.
                partition_mounts += 1
                mountpoint = "/media/partition_%d" % partition_mounts

            if "boot" in partition_info:
                boot = True
            else:
                boot = False

            msger.verbose("Size: %s Bytes, fstype: %s, mountpoint: %s, boot: %s" % (size, fstype, mountpoint, boot))
            # TODO: add_partition should take bytes as size parameter.
            imgloop.add_partition((int)(size)/1024/1024, "/dev/sdb", mountpoint, fstype = fstype, boot = boot)

        try:
            imgloop.mount()

        except errors.MountError:
            imgloop.cleanup()
            raise

        try:
            chroot.chroot(imgmnt, None,  "/bin/env HOME=/root /bin/bash")
        except:
            raise errors.CreatorError("Failed to chroot to %s." %img)
        finally:
            chroot.cleanup_after_chroot("img", imgloop, None, imgmnt)

    @classmethod
    def do_unpack(cls, srcimg):
        srcimgsize = (misc.get_file_size(srcimg)) * 1024L * 1024L
        srcmnt = misc.mkdtemp("srcmnt")
        disk = fs_related.SparseLoopbackDisk(srcimg, srcimgsize)
        srcloop = PartitionedMount({'/dev/sdb':disk}, srcmnt, skipformat = True)

        srcloop.add_partition(srcimgsize/1024/1024, "/dev/sdb", "/", "ext3", boot=False)
        try:
            srcloop.mount()

        except errors.MountError:
            srcloop.cleanup()
            raise

        image = os.path.join(tempfile.mkdtemp(dir = "/var/tmp", prefix = "tmp"), "target.img")
        args = ['dd', "if=%s" % srcloop.partitions[0]['device'], "of=%s" % image]

        msger.info("`dd` image ...")
        rc = runner.show(args)
        srcloop.cleanup()
        shutil.rmtree(os.path.dirname(srcmnt), ignore_errors = True)

        if rc != 0:
            raise errors.CreatorError("Failed to dd")
        else:
            return image
