from pathlib import Path

from house_qt import file_drop


class FakeCapsulePage:
    def __init__(self):
        self.paths = []

    def show(self):
        pass

    def install_paths(self, paths):
        self.paths.extend(paths)


class FakeMigrationPage:
    def __init__(self):
        self.paths = []

    def show(self):
        pass

    def inspect_paths(self, paths):
        self.paths.extend(paths)


class FakeWindow:
    def __init__(self):
        self.pages = {"capsulePage": FakeCapsulePage(), "migrationPage": FakeMigrationPage()}
        self.switched = []

    def switchTo(self, page):
        self.switched.append(page)


def test_classify_supported_files(tmp_path):
    assert file_drop.classify(tmp_path / "a.oqtc") == "capsule"
    assert file_drop.classify(tmp_path / "a.oqdbackup") == "backup"
    assert file_drop.classify(tmp_path / "chatgpt.zip") == "migration"
    assert file_drop.classify(tmp_path / "conversations.json") == "migration"
    assert file_drop.classify(tmp_path) == "migration"
    assert file_drop.classify(tmp_path / "a.exe") == ""


def test_queue_and_drain_roundtrip(tmp_path):
    one, two = tmp_path / "a.oqtc", tmp_path / "b.json"
    one.write_text("x", "utf-8")
    two.write_text("{}", "utf-8")
    assert len(file_drop.queue_open(tmp_path, [one, two, one])) == 2
    drained = file_drop.drain_open(tmp_path)
    assert {item.name for item in drained} == {"a.oqtc", "b.json"}
    assert file_drop.drain_open(tmp_path) == []


def test_route_sends_each_kind_to_its_page(tmp_path):
    window = FakeWindow()
    capsule, export = tmp_path / "task.oqtc", tmp_path / "export.json"
    capsule.write_text("x", "utf-8")
    export.write_text("{}", "utf-8")
    assert file_drop.route(window, [capsule, export]) == 2
    assert window.pages["capsulePage"].paths == [capsule.resolve()]
    assert window.pages["migrationPage"].paths == [export.resolve()]
