import inspect
import unittest

import desktop


class AppLaunchContractTests(unittest.TestCase):
    def test_launcher_discovers_start_apps_and_verifies_visible_result(self):
        source = inspect.getsource(desktop.launch)
        self.assertIn("Get-StartApps", source)
        self.assertIn("shell:AppsFolder", source)
        self.assertIn("no new foreground window was observed", source)
        self.assertNotIn("cmd\", \"/c\", \"start", source)


if __name__ == "__main__":
    unittest.main()
