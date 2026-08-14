import unittest

from lib.git_remote import parse_remote


class GitRemoteTest(unittest.TestCase):
    def test_parses_https_remote(self) -> None:
        remote = parse_remote("https://gitlab.example.com/group/project.git")

        self.assertEqual("gitlab.example.com", remote.host)
        self.assertEqual("group/project", remote.repository_path)

    def test_parses_ssh_remote_with_port(self) -> None:
        remote = parse_remote("ssh://git@gitlab.example.com:2222/group/project.git")

        self.assertEqual("gitlab.example.com", remote.host)
        self.assertEqual("group/project", remote.repository_path)

    def test_parses_scp_like_remote(self) -> None:
        remote = parse_remote("git@gitlab.example.com:group/project.git")

        self.assertEqual("gitlab.example.com", remote.host)
        self.assertEqual("group/project", remote.repository_path)

    def test_preserves_local_repository_path(self) -> None:
        remote = parse_remote("/tmp/group/project.git")

        self.assertIsNone(remote.host)
        self.assertEqual("/tmp/group/project", remote.repository_path)


if __name__ == "__main__":
    unittest.main()
