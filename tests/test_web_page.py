import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parent.parent / "web" / "prototype" / "index.html"


class WebPageTest(unittest.TestCase):
    def setUp(self):
        self.html = PAGE.read_text(encoding="utf-8")

    def test_one_export_placeholder(self):
        self.assertEqual(self.html.count("/*__EXPORT__*/"), 1)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_script_parses(self):
        # The page script is written by hand; a syntax error would leave the whole site blank.
        script = self.html[self.html.rindex("<script>") + len("<script>"):self.html.rindex("</script>")]
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as f:
            f.write(script)
        try:
            proc = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
        finally:
            Path(f.name).unlink()
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
