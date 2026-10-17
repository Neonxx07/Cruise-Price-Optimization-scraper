"""A saved booking list per cruise line, de-duplicated silently, 2026-10-16.

Neon: "everyday i keep opening copy paste for 4 cruise lines and we are
basically checking the same booking numbers everyday can you make a saving
list for each cruise line and when i enter a new list of booking or a single
booking the gui know if it is duplicated and just does [not] do a warning or
anything but silentley dedplicate".
"""
import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from core.models import CruiseLine
from gui.saved_list import SavedList, parse_ids


def test_parse_accepts_commas_spaces_semicolons_and_new_lines():
    assert parse_ids("3001041, 3001035\n zm52l7 ;DEMO15\t\n\n") == [
        "3001041", "3001035", "DEMO41", "DEMO15"]


def test_duplicates_are_dropped_silently_and_order_is_kept(tmp_path):
    lst = SavedList("GOCCL", root=tmp_path)
    assert lst.add(["DEMO41", "DEMO15"]) == ["DEMO41", "DEMO15"]
    assert lst.add(["cd1l69 ", "DEMO41", "DEMO16", "DEMO16"]) == ["DEMO16"]
    assert lst.load() == ["DEMO41", "DEMO15", "DEMO16"]
    assert (tmp_path / "GOCCL.txt").read_text(encoding="utf-8").split() == lst.load()


def test_each_line_has_its_own_list(tmp_path):
    SavedList("NCL", root=tmp_path).add(["3001052"])
    assert SavedList("ESPRESSO", root=tmp_path).load() == []
    assert SavedList("NCL", root=tmp_path).load() == ["3001052"]


def test_a_hand_edited_file_is_read_with_its_duplicates_ignored(tmp_path):
    (tmp_path / "PRINCESS.txt").write_text("# my list\nDEMO12\ndemo12\n\nDEMO53\n", encoding="utf-8")
    assert SavedList("PRINCESS", root=tmp_path).load() == ["DEMO12", "DEMO24"]


def test_a_removal_is_logged_never_lost(tmp_path):
    lst = SavedList("ESPRESSO", root=tmp_path)
    lst.add(["1", "2", "3"])
    assert lst.remove(["2"]) == ["2"]
    assert lst.load() == ["1", "3"]
    assert "\t2" in (tmp_path / "ESPRESSO.removed.log").read_text(encoding="utf-8")


# -- in the GUI --------------------------------------------------------------

@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(qt_app, tmp_path, monkeypatch):
    from gui.windows import CruiseLinePanel

    popups = []
    for kind in ("warning", "information", "critical", "question"):
        monkeypatch.setattr(QMessageBox, kind, lambda *a, _k=kind, **k: popups.append(_k))
    p = CruiseLinePanel(CruiseLine.NCL)
    p.saved_list = SavedList("NCL", root=tmp_path)
    p._refresh_saved_list_label()
    p._popups = popups
    return p


def _queued(p):
    return [i.booking_id for i in p.queue_manager.get_snapshot().items]


def test_pasting_duplicates_queues_each_once_saves_each_once_and_never_pops_up(panel):
    panel.bulk_input.setPlainText("3001052, 3001055\n3001052\n3001053")
    panel._add_bulk()
    panel.bulk_input.setPlainText("3001055\n3001054")
    panel._add_bulk()
    panel.booking_input.setText("3001052")
    panel._add_booking()
    assert _queued(panel) == ["3001052", "3001055", "3001053", "3001054"]
    assert panel.saved_list.load() == ["3001052", "3001055", "3001053", "3001054"]
    assert panel._popups == []
    assert "Load saved list (4)" == panel.load_saved_button.text()


def test_load_saved_list_queues_everything_skipping_what_is_queued(panel):
    panel.saved_list.add(["3001052", "3001055", "3001053"])
    panel.booking_input.setText("3001055")
    panel._add_booking()
    panel._load_saved_list()
    assert _queued(panel) == ["3001055", "3001052", "3001053"]
    assert panel._popups == []
    assert "1 already in the queue" in panel.status_label.text()
