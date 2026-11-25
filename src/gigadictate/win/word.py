"""Word's document canvas exposes editable ranges through its own automation API."""
from contextlib import contextmanager
from os.path import commonprefix


@contextmanager
def active_selection(hwnd):
    from comtypes.client import GetActiveObject
    from uiautomation import UIAutomationInitializerInThread
    from . import api

    with UIAutomationInitializerInThread():
        app = GetActiveObject("Word.Application", dynamic=True)
        window = app.ActiveWindow
        if int(window.Hwnd) != hwnd or api.GetForegroundWindow() != hwnd:
            raise ValueError("Фокус изменился")
        yield window.Document, window.Selection


class WordDraft:
    @classmethod
    def open(cls, hwnd):
        try:
            with active_selection(hwnd) as (doc, selection):
                # Only start in an ordinary insertion point. Leave selected rich
                # content intact until the user commits via normal typing.
                if doc.ReadOnly or selection.Type != 1:
                    return None
                draft = cls()
                draft.target = hwnd
                draft.document = doc.FullName
                draft.story = selection.StoryType
                draft.start = draft.end = selection.Start
                draft.text = ""
                return draft
        except Exception:
            return None

    def replace(self, text, restore=False):
        try:
            with active_selection(self.target) as (doc, selection):
                if (doc.FullName != self.document or selection.StoryType != self.story
                        or (selection.Start, selection.End) != (self.end, self.end)):
                    return False
                span = selection.Range.Duplicate
                span.SetRange(self.start, self.end)
                if (span.Text or "") != self.text:
                    return False
                updated = "" if restore else text.replace("\n", "\r")
                if self.text != updated:
                    keep = len(commonprefix([self.text, updated]))
                    # Word offsets are UTF-16 code units. Preserve the stable prefix.
                    span.SetRange(self.start + len(updated[:keep].encode("utf-16-le")) // 2, self.end)
                    span.Text = updated[keep:]
                    selection.SetRange(span.End, span.End)
                    self.end = span.End
                    self.text = updated
                return True
        except Exception:
            return False
