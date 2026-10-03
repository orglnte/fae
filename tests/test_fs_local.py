"""Whether the workspaces sit on a local disk, read from `mount`'s output."""
import unittest

from fae import mutex


class TestParseMount(unittest.TestCase):

    def test_a_linux_network_mount_is_not_local(self):
        self.assertEqual(mutex.parse_mount("srv:/x on /mnt/w type nfs4 (rw,relatime)", "/mnt/w"),
                         (False, "nfs4"))

    def test_a_linux_disk_is_local(self):
        self.assertEqual(mutex.parse_mount("/dev/sda1 on / type ext4 (rw,relatime)", "/"),
                         (True, "ext4"))

    def test_a_macos_share_is_not_local(self):
        line = "//u@h/s on /Volumes/s (smbfs, nodev, nosuid, mounted by u)"
        self.assertEqual(mutex.parse_mount(line, "/Volumes/s"), (False, "smbfs"))

    def test_a_macos_disk_marked_local_is_local(self):
        line = "/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled, nobrowse)"
        self.assertEqual(mutex.parse_mount(line, "/System/Volumes/Data"), (True, "apfs"))

    def test_a_macos_mount_without_the_local_option_is_not_local(self):
        line = "map auto_home on /System/Volumes/Data/home (autofs, automounted, nobrowse)"
        self.assertEqual(mutex.parse_mount(line, "/System/Volumes/Data/home"), (False, "autofs"))

    def test_an_unlisted_mount_point_cannot_be_told(self):
        self.assertEqual(mutex.parse_mount("/dev/sda1 on / type ext4 (rw)", "/mnt/x"), (None, "?"))


if __name__ == "__main__":
    unittest.main()
