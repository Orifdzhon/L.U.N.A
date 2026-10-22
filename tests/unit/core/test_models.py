import dataclasses

import pytest

from luna.core.models import Reply, Request, new_id


def test_request_is_immutable():
    req = Request(text="open notepad", source="text")
    with pytest.raises(dataclasses.FrozenInstanceError):
        req.text = "other"  # type: ignore[misc]


def test_request_ids_are_unique():
    ids = {Request(text="x", source="voice").request_id for _ in range(1000)}
    assert len(ids) == 1000


def test_request_defaults():
    req = Request(text="hi", source="voice", lang="ru")
    assert req.lang == "ru"
    assert Request(text="hi", source="text").lang is None


def test_new_id_is_nonempty_string():
    assert isinstance(new_id(), str) and new_id()


def test_reply_defaults_do_not_share_actions():
    a = Reply(text="a", lang="en")
    b = Reply(text="b", lang="en")
    a.actions.append("x")  # type: ignore[arg-type]
    assert b.actions == []
    assert a.provider is None
