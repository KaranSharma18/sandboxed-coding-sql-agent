from textkit import slugify


def test_basic():
    assert slugify("Hello, World!") == "hello-world"


def test_collapses_separators():
    assert slugify("  many   spaces -- and_underscores ") == "many-spaces-and-underscores"


def test_drops_accents():
    assert slugify("Crème brûlée") == "creme-brulee"


def test_max_length_does_not_cut_a_word_in_half():
    assert slugify("one two three four", max_length=10) == "one-two"


def test_empty():
    assert slugify("!!!") == ""
