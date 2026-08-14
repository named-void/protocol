import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "protocol"
    / "scripts"
    / "task_stage.py"
)


class TaskStageTest(unittest.TestCase):
    STAGES = (
        "stage=0 name=preflight check=none\n",
        "stage=1 name=task-definition check=none\n",
        "stage=2 name=discovery check=required\n",
        "stage=3 name=adr check=conditional\n",
        "stage=4 name=planning check=required\n",
        "stage=5 name=implementation check=required\n",
        "stage=6 name=acceptance check=none\n",
        "stage=7 name=completed check=none\n",
    )

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.task_dir = Path(self.temporary_directory.name)
        self.stage_file = self.task_dir / "task_stage.md"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_command(self, *arguments: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *(str(argument) for argument in arguments)],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_linear_lifecycle(self) -> None:
        self.assertEqual(self.run_command("init", self.task_dir).returncode, 0)
        self.assertEqual(
            self.run_command("current", self.task_dir).stdout,
            "stage=0\n",
        )
        for stage, description in enumerate(self.STAGES[:-1]):
            self.assertEqual(self.stage_file.read_bytes(), f"{stage}\n".encode("ascii"))
            self.assertEqual(
                self.run_command("describe", self.task_dir).stdout,
                description,
            )
            self.assertEqual(
                self.run_command("require", self.task_dir, stage).returncode,
                0,
            )
            self.assertEqual(
                self.run_command("advance", self.task_dir, stage).returncode,
                0,
            )

        self.assertEqual(self.stage_file.read_bytes(), b"7\n")
        self.assertEqual(
            self.run_command("describe", self.task_dir).stdout,
            self.STAGES[-1],
        )
        self.assertNotEqual(self.run_command("advance", self.task_dir, 7).returncode, 0)

    def test_invalid_state_cannot_reset_or_advance(self) -> None:
        self.assertEqual(self.run_command("init", self.task_dir).returncode, 0)
        self.assertNotEqual(self.run_command("init", self.task_dir).returncode, 0)
        self.assertEqual(self.run_command("advance", self.task_dir, 0).returncode, 0)
        self.assertNotEqual(self.run_command("advance", self.task_dir, 0).returncode, 0)
        self.assertEqual(self.stage_file.read_bytes(), b"1\n")

        self.stage_file.write_bytes(b"1\nextra\n")
        self.assertNotEqual(self.run_command("require", self.task_dir, 1).returncode, 0)

    def test_only_acceptance_can_return_to_implementation(self) -> None:
        self.stage_file.write_bytes(b"6\n")
        self.assertEqual(
            self.run_command("return-to-implementation", self.task_dir).returncode,
            0,
        )
        self.assertEqual(self.stage_file.read_bytes(), b"5\n")
        self.assertNotEqual(
            self.run_command("return-to-implementation", self.task_dir).returncode,
            0,
        )


if __name__ == "__main__":
    unittest.main()
