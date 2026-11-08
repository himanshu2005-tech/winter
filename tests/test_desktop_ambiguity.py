import unittest
from unittest import mock

import desktop


class Node:
    def __init__(self, name, kind, enabled=True):
        self.Name, self.ControlTypeName, self.IsEnabled = name, kind, enabled

    def GetChildren(self):
        return []


class Root(Node):
    def __init__(self, children):
        super().__init__("root", "WindowControl")
        self._children = children

    def GetChildren(self):
        return self._children


class FakeAuto:
    def __init__(self, nodes):
        self.root = Root(nodes)

    def GetForegroundControl(self):
        return self.root

    def GetRootControl(self):
        return self.root


class Ambiguity(unittest.TestCase):
    def find(self, nodes, name, kind=""):
        with mock.patch.object(desktop, "_uia", return_value=FakeAuto(nodes)):
            return desktop.find_control(name, kind)

    def test_one_name_two_kinds_is_not_guessed(self):
        control, error = self.find([Node("Replace", "EditControl"),
                                    Node("Replace", "ButtonControl")], "Replace")
        self.assertIsNone(control)
        self.assertIn("more than one kind", error)
        self.assertIn("Button", error)

    def test_a_control_type_resolves_it(self):
        control, error = self.find([Node("Replace", "EditControl"),
                                    Node("Replace", "ButtonControl")], "Replace", "Button")
        self.assertIsNone(error)
        self.assertEqual(control.ControlTypeName, "ButtonControl")

    def test_the_same_kind_twice_still_picks_the_first(self):
        control, error = self.find([Node("Close", "ButtonControl"),
                                    Node("Close", "ButtonControl")], "Close")
        self.assertIsNone(error)

    def test_a_unique_name_is_unaffected(self):
        control, error = self.find([Node("Save", "ButtonControl")], "Save")
        self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
