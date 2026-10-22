import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from paper_translate import chunker, cli, latex, project, prompts, texutil  # noqa: E402
from paper_translate.validate import validate  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "sample_paper"
FAKE_ENGINE = ROOT / "tests" / "fake_engine.py"


class TexUtilTest(unittest.TestCase):
    def test_comment_start_ignores_escaped_percent(self):
        self.assertEqual(texutil.comment_start(r"50\% of data % note"), 13)
        self.assertEqual(texutil.comment_start(r"line ends with \\% comment"), 17)
        self.assertEqual(texutil.comment_start(r"no comment \%"), -1)

    def test_remove_comment_lines_keeps_verbatim(self):
        text = "a\n% drop\n  % drop too\nb % keep\n\\begin{verbatim}\n% kept\n\\end{verbatim}\n"
        self.assertEqual(texutil.remove_comment_lines(text),
                         "a\nb % keep\n\\begin{verbatim}\n% kept\n\\end{verbatim}\n")

    def test_protect_and_restore(self):
        text = "x \\verb|a%b| y\n\\begin{lstlisting}\ncode\n\\end{lstlisting}\nz"
        masked, regions = texutil.protect_regions(text)
        self.assertNotIn("lstlisting", masked)
        self.assertNotIn("a%b", masked)
        self.assertEqual(texutil.restore_regions(masked, regions), text)

    def test_documentclass_span(self):
        text = "% \\documentclass{fake}\n\\documentclass[a4paper,\n 11pt]{article}\n\\begin{document}"
        start, end, name = texutil.documentclass_span(text)
        self.assertEqual(name, "article")
        self.assertTrue(text[start:end].startswith("\\documentclass[a4paper"))
        self.assertTrue(text[start:end].endswith("{article}"))

    def test_key_counter(self):
        text = r"see \cref{a,b} and \citep[p.~3]{k1, k2} \label{l}"
        self.assertEqual(set(texutil.key_counter(text, "ref")), {"a", "b"})
        self.assertEqual(set(texutil.key_counter(text, "cite")), {"k1", "k2"})
        self.assertEqual(set(texutil.key_counter(text, "label")), {"l"})


class ChunkerTest(unittest.TestCase):
    def test_chunks_reassemble_to_original(self):
        text = (FIXTURE / "main.tex").read_text(encoding="utf-8")
        unit = chunker.split_file("main.tex", text, True, max_chars=300, remove_comments=False)
        self.assertGreater(len(unit.chunks), 3)
        self.assertEqual(unit.assemble([c.text for c in unit.chunks]), text)
        self.assertEqual(unit.chunks[0].kind, "preamble")
        self.assertTrue(unit.chunks[-1].text.rstrip().endswith("\\end{document}"))

    def test_preamble_without_text_is_not_translated(self):
        text = "\\documentclass{article}\n\\usepackage{x}\n\\begin{document}\nHello world, this is text.\n\\end{document}\n"
        unit = chunker.split_file("m.tex", text, True, max_chars=1000)
        self.assertEqual([(c.kind, c.translate) for c in unit.chunks],
                         [("preamble", False), ("body", True)])

    def test_merge_translation_keeps_surrounding_whitespace(self):
        self.assertEqual(chunker.merge_translation("\nabc\n\n", "訳\n"), "\n訳\n\n")


class ValidateTest(unittest.TestCase):
    SRC = ("We show \\cref{fig:a} in Section~\\ref{sec:b}, following \\cite{k}.\n"
           "\\begin{equation}x = 1\\label{eq:x}\\end{equation}\n"
           "Here $a$ and $b$ are scalars used by the model in every experiment. [[[KEEP_0000]]]\n")

    def test_good_translation(self):
        out = ("\\cref{fig:a} と \\ref{sec:b} 節に示す\\cite{k}．\n"
               "\\begin{equation}x = 1\\label{eq:x}\\end{equation}\n"
               "ここで $a$ と $b$ はすべての実験でモデルが用いるスカラーである．[[[KEEP_0000]]]\n")
        r = validate(self.SRC, out)
        self.assertTrue(r.ok, r.errors)
        self.assertEqual(r.warnings, [])

    def test_broken_translation(self):
        out = "\\cref{fig:a} を示す．\\begin{equation}x = 1\\end{equation} $a$ と b はスカラー．\n"
        r = validate(self.SRC, out)
        joined = " ".join(r.errors)
        self.assertIn("KEEP", joined)
        self.assertIn("$", joined)
        self.assertIn("label", joined)
        self.assertTrue(any("cite" in w for w in r.warnings))

    def test_untranslated_output(self):
        src = self.SRC + "These results suggest that the proposed estimator is robust to noise.\n"
        r = validate(src, src)
        self.assertEqual(r.errors, ["日本語に翻訳されていない．"])


class ProjectTest(unittest.TestCase):
    def test_analyze_fixture(self):
        proj = project.analyze(FIXTURE)
        self.assertEqual(proj.main.name, "main.tex")
        self.assertEqual([proj.rel(p) for p in proj.files], ["main.tex", "sections/intro.tex"])
        self.assertEqual(sorted(proj.rel(p) for p in proj.skipped),
                         ["figures/overview.tex", "macros.tex"])

    def test_extract_zip_with_wrapping_directory(self):
        with tempfile.TemporaryDirectory() as d:
            archive = Path(d) / "paper.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("paper/main.tex", "\\documentclass{article}\\begin{document}x\\end{document}")
            root = project.extract_source(archive, Path(d) / "src")
            self.assertEqual(root.name, "paper")
            self.assertTrue((root / "main.tex").exists())

    def test_extract_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as d:
            archive = Path(d) / "evil.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("../evil.tex", "x")
            with self.assertRaises(project.ProjectError):
                project.extract_source(archive, Path(d) / "src")

    def test_context_extraction(self):
        text = (FIXTURE / "main.tex").read_text(encoding="utf-8")
        self.assertEqual(project.extract_title(text), "Sparse Feature Selection with Adaptive Thresholds")
        self.assertTrue(project.extract_abstract(text).startswith("We study feature selection"))
        self.assertEqual(project.extract_headings(text), ["Method", "Experiments"])


class PromptsTest(unittest.TestCase):
    def test_parse_translation(self):
        resp = "前置き\n<<<TRANSLATION_BEGIN>>>\n```latex\n本文\n```\n<<<TRANSLATION_END>>>\n"
        self.assertEqual(prompts.parse_translation(resp), "本文")
        with self.assertRaises(prompts.ResponseFormatError):
            prompts.parse_translation("<<<TRANSLATION_BEGIN>>>\n途中で切れた")

    def test_glossary_merge_prefers_user_entries(self):
        auto = prompts.Glossary.parse("kernel\tカーネル\t\nattention\tattention\t\n")
        user = prompts.Glossary.parse("# comment\nKernel\t核\t積分核の意味\n")
        merged = auto.merged_with(user)
        self.assertEqual(merged.entries[0][:2], ("Kernel", "核"))
        self.assertEqual(len(merged.entries), 2)
        self.assertIn("attention → （英語のまま）", merged.render())

    def test_guidelines_punctuation_and_extra(self):
        g = prompts.load_guidelines(None, "touten-kuten", ["数式番号は残す．"])
        self.assertIn("読点と句点（、。）", g)
        self.assertNotIn("{{PUNCTUATION}}", g)
        self.assertTrue(g.rstrip().endswith("数式番号は残す．"))


class LatexTest(unittest.TestCase):
    def test_inject_japanese_support(self):
        text = "\\pdfoutput=1\n\\documentclass{article}\n\\usepackage{x}\n\\begin{document}\n本文\n\\end{document}\n"
        out = latex.inject_japanese_support(text)
        self.assertIn("\\documentclass{article}\n\\usepackage{luatexja}\n\\usepackage{x}", out)
        self.assertLess(out.index("\\figurename}{図}"), out.index("\\begin{document}"))
        self.assertTrue(out.startswith(latex.ENGINE_COMPAT))

    def test_no_injection_for_japanese_class(self):
        text = "\\documentclass{ltjsarticle}\n\\begin{document}\n\\end{document}\n"
        out = latex.inject_japanese_support(text, localize_names=False)
        self.assertNotIn("\\usepackage{luatexja}", out)


class EndToEndTest(unittest.TestCase):
    def test_translate_with_fake_engine(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            archive = d / "sample.zip"
            shutil.make_archive(str(archive.with_suffix("")), "zip", FIXTURE)
            args = [str(archive), "-o", str(d / "out"), "--engine", "command",
                    "--engine-cmd", f"{sys.executable} {FAKE_ENGINE}", "--no-build", "-j", "2"]
            self.assertEqual(cli.main(args), 0)

            work = d / "out" / "sample_ja_work"
            main_tex = (work / "ja" / "main.tex").read_text(encoding="utf-8")
            self.assertIn("\\usepackage{luatexja}", main_tex)
            self.assertIn("【訳】We study feature selection", main_tex)
            self.assertIn("# 100% vectorized", main_tex)                   # verbatim は保持
            self.assertNotIn("This comment line is removed", main_tex)      # コメント行は削除
            self.assertNotIn("【訳】", (work / "ja" / "figures" / "overview.tex").read_text(encoding="utf-8"))
            self.assertTrue((d / "out" / "sample_ja_src.zip").exists())
            report = json.loads((work / "report.json").read_text(encoding="utf-8"))
            self.assertTrue(all(r["status"] == "translated" for r in report))

            # 2 回目はキャッシュから再利用される
            self.assertEqual(cli.main(args), 0)
            self.assertTrue(any(f.suffix == ".json" for f in (work / "cache").iterdir()))


if __name__ == "__main__":
    unittest.main()
