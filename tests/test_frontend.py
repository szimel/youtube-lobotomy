"""Checks that the page and the script still agree with each other.

A dashboard held together by `querySelector("#id")` fails silently when a name
drifts: the element is null, the first interaction throws, and the page looks
frozen rather than broken. These tests fail loudly instead.
"""

import re
import tempfile
import unittest
from pathlib import Path

from app import create_app


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "static" / "script.js"
TEMPLATE = ROOT / "templates" / "index.html"

ID_ATTRIBUTE = re.compile(r'id="([^"]+)"')
ID_SELECTOR = re.compile(r'querySelector\(\s*"#([A-Za-z0-9_-]+)"')


def function_source(script: str, name: str) -> str:
    """One top-level function, up to the closing brace in the first column."""
    match = re.search(
        rf"^(?:async )?function {re.escape(name)}\(.*?\n\}}\n", script, re.S | re.M
    )
    if match is None:
        raise AssertionError(f"{name} should be defined at the top level")
    return match.group(0)


class FrontendTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        app = create_app(
            {"TESTING": True, "DATA_DIR": Path(self.temporary_directory.name)}
        )
        self.client = app.test_client()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def rendered_page(self) -> str:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def test_every_element_the_script_selects_exists_in_the_page(self):
        page = self.rendered_page()
        available = set(ID_ATTRIBUTE.findall(page))
        wanted = set(ID_SELECTOR.findall(SCRIPT.read_text(encoding="utf-8")))

        self.assertTrue(wanted, "the script should select something")
        self.assertEqual(
            sorted(wanted - available),
            [],
            "the script selects elements the page does not define",
        )

    def test_every_referenced_id_exists(self):
        # aria-labelledby and <label for> point at ids too, and a rename that
        # misses one of those leaves the page inaccessible rather than broken.
        page = self.rendered_page()
        available = set(ID_ATTRIBUTE.findall(page))
        referenced = set()
        for attribute in ("aria-labelledby", "aria-describedby", "for"):
            referenced |= set(
                re.findall(rf'{attribute}="([^"]+)"', page)
            )

        self.assertEqual(
            sorted(referenced - available),
            [],
            "the page points at ids it does not define",
        )

    def test_every_named_element_the_script_uses_is_declared(self):
        # `elements.whatever` gives no error until it is used, and then only in
        # the browser, so the map and its uses are compared here instead.
        script = SCRIPT.read_text(encoding="utf-8")
        declaration = re.search(r"const elements = \{(.*?)\n\};", script, re.S)
        self.assertIsNotNone(declaration, "the elements map should be a literal")
        declared = set(
            re.findall(r"^\s*([A-Za-z0-9_]+):", declaration.group(1), re.M)
        )

        used = set(re.findall(r"elements\.([A-Za-z0-9_]+)", script))

        self.assertTrue(used, "the script should use the elements map")
        self.assertEqual(
            sorted(used - declared),
            [],
            "the script uses elements that the map does not declare",
        )

    def test_assets_are_served(self):
        for path in ("/static/script.js", "/static/style.css"):
            response = self.client.get(path)
            # Leaving it to the garbage collector makes the suite print
            # ResourceWarnings about the file the response is still holding open.
            response.close()
            self.assertEqual(response.status_code, 200, path)

    def test_the_template_does_not_repeat_an_id(self):
        template_ids = ID_ATTRIBUTE.findall(TEMPLATE.read_text(encoding="utf-8"))
        duplicates = {name for name in template_ids if template_ids.count(name) > 1}
        self.assertEqual(sorted(duplicates), [], "the template repeats an id")

    def test_saving_from_the_feed_adds_the_card_to_watch_later(self):
        # The feed and Watch later are separate lists. Saving used to update the
        # file and the feed card's button but never the Watch later list, so a
        # saved video only appeared after a reload: correct state, stale screen.
        script = SCRIPT.read_text(encoding="utf-8")

        saving = function_source(script, "addToWatchLater")
        self.assertIn(
            "addWatchLaterCard(",
            saving,
            "saving must add a card, not just update the file",
        )

        adding = function_source(script, "addWatchLaterCard")
        self.assertIn("elements.watchLaterList", adding)
        self.assertIn("createVideoCard(video, true)", adding)
        self.assertIn(
            'querySelector(".empty-state")',
            adding,
            "the empty message must not be left above the card",
        )
        self.assertIn("updateWatchLaterCount()", adding)

    def test_the_count_is_written_in_one_place(self):
        # Three places used to build "N saved" by hand, and the newest one
        # forgot to: the number under the heading is what tells you the save
        # worked, so it comes from one function.
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn(
            "updateWatchLaterCount()",
            function_source(script, "render"),
        )
        self.assertIn(
            "updateWatchLaterCount()",
            function_source(script, "removeFromWatchLater"),
        )
        self.assertEqual(
            script.count("saved`"),
            1,
            "only updateWatchLaterCount should build the count text",
        )

    def test_playback_is_never_started_programmatically(self):
        # YouTube counts a view only when playback begins at its own play
        # button: "A playback only counts toward a video's official view count
        # if it is initiated via a native play button in the player." Calling
        # playVideo() -- or autoplaying -- would play the video without it ever
        # registering, which defeats the reason for watching it here.
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIsNone(
            re.search(r"\.playVideo\s*\(", script),
            "the player must not be started from code",
        )
        self.assertNotIn("autoplay: 1", script)


if __name__ == "__main__":
    unittest.main()
