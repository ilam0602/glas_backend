from server import story_blob_path, is_story_owner


def test_story_blob_path_strips_media_prefix():
    assert story_blob_path("/media/stories/uid/sid.jpg") == "stories/uid/sid.jpg"


def test_story_blob_path_strips_only_first_occurrence():
    assert story_blob_path("/media/a/media/b.jpg") == "a/media/b.jpg"


def test_is_story_owner_true_when_uid_matches():
    assert is_story_owner({"userId": "u1"}, "u1") is True


def test_is_story_owner_false_when_uid_differs():
    assert is_story_owner({"userId": "u1"}, "u2") is False


def test_is_story_owner_false_when_uid_empty():
    assert is_story_owner({"userId": "u1"}, "") is False
