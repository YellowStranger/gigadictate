from contextlib import contextmanager
from types import SimpleNamespace

from gigadictate.win import word


class Selection:
    Type = 1
    StoryType = 1

    def __init__(self):
        self.Start = self.End = 4
        self.document = "До: |конец"

    def SetRange(self, start, end):
        self.Start, self.End = start, end

    @property
    def Range(self):
        selection = self

        class Range:
            def SetRange(self, start, end):
                self.start, self.End = start, end

            @property
            def Text(self):
                return selection.document[self.start:self.End]

            @Text.setter
            def Text(self, value):
                selection.document = selection.document[:self.start] + value + selection.document[self.End:]
                self.End = self.start + len(value)

        return SimpleNamespace(Duplicate=Range())


def test_word_draft_revises_only_its_range_and_restores_on_cancel(monkeypatch):
    sel = Selection()
    @contextmanager
    def active(hwnd):
        yield SimpleNamespace(ReadOnly=False, FullName="Документ"), sel
    monkeypatch.setattr(word, "active_selection", active)
    draft = word.WordDraft.open(1)
    assert draft.replace("черновик") and draft.replace("готовый текст")
    assert sel.document == "До: готовый текст|конец"
    assert draft.replace("", restore=True) and sel.document == "До: |конец"
    sel.SetRange(2, 2)
    assert not draft.replace("лишний текст")
    assert sel.document == "До: |конец"
