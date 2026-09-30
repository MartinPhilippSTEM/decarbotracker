import pytest
from conftest import make_item

from decarbotracker.filter import default_matcher, normalize_text, prefilter


@pytest.mark.parametrize("text", [
    "Prodej tepelných čerpadel v Česku klesl",
    "Energetická chudoba se týká milionu lidí",
    "Nový průzkum: postoje Čechů ke klimatické politice",
    "Povolenky ETS2 zdraží vytápění",
    "CBAM začne platit naplno",
    "Spravedlivá transformace uhelných regionů",
    "Obnovitelné zdroje dodaly rekordní podíl elektřiny",
    "Fotovoltaika na střechách",
    "Public opinion on net zero policies in the UK",
    "New poll shows attitudes toward heat pumps",
    "Emissions forecast for 2030",
])
def test_keywords_match_czech_inflection_and_english(text):
    assert default_matcher().matches(text), text


@pytest.mark.parametrize("text", [
    "Fotbalová liga: Sparta porazila Slavii",
    "Nová kavárna v centru Brna",
    "Stock market closes higher on tech rally",
])
def test_keywords_do_not_match_offtopic(text):
    assert not default_matcher().matches(text), text


def test_normalize_text_removes_diacritics():
    assert normalize_text("Žluťoučký kůň ÚPĚL") == "zlutoucky kun upel"


def test_exact_terms_need_whole_word():
    m = default_matcher()
    assert m.matches("Reforma EU ETS")
    assert not m.matches("Sets of photos from the exhibition")


def test_prefilter_topic_filter_and_exclusions():
    pure = make_item(1, title="Weekly roundup", topic_filter=False)
    media_off = make_item(2, title="Fotbalová liga pokračuje", topic_filter=True)
    media_on = make_item(3, title="Ceny elektřiny příští rok porostou", topic_filter=True)
    job = make_item(4, title="Hledáme kolegyni/kolegu do kampaně Klima", topic_filter=False)
    out = prefilter([pure, media_off, media_on, job])
    assert out == [pure, media_on]


def test_categories():
    cats = default_matcher().categories_for("Průzkum veřejného mínění ukázal podporu tepelných čerpadel")
    assert "public_attitudes" in cats and "technology" in cats
