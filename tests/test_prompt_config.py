import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from node_index import tree
from node_index.db import CliError, DEFAULT_PROMPT_CONFIG


class PromptConfig(unittest.TestCase):
    def cfg_with(self, **over):
        cfg = tree.load_prompt_config(DEFAULT_PROMPT_CONFIG)
        cfg.update(over)
        return cfg

    def test_text_comes_from_yaml_not_code(self):
        doc = tree.prompt_doc([], 3, self.cfg_with(title="自定义标题", intro="自定义开头"))
        self.assertTrue(doc.startswith("# 自定义标题\n\n自定义开头\n"))

    def test_placeholders_filled(self):
        doc = tree.prompt_doc([], 3, self.cfg_with(notes=["深度={depth}", "命令={exe}"]))
        self.assertIn("- 深度=3", doc)
        self.assertIn("bin/node-index", doc)
        self.assertNotIn("{exe}", doc)

    def test_missing_file_is_error_not_silent_fallback(self):
        with self.assertRaises(CliError):
            tree.load_prompt_config(Path(tempfile.gettempdir()) / "no-such-prompt.yml")

    def test_bad_field_type_is_error(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "p.yml"
            p.write_text("title: x\n", encoding="utf-8")
            with self.assertRaises(CliError):
                tree.load_prompt_config(p)


if __name__ == "__main__":
    unittest.main()
