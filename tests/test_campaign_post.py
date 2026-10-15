"""The post text per platform and the YouTube title (Brief 11.2). Invented words only."""

from pathlib import Path
from typing import Any

import pytest

from campaign_helpers import base_campaign, moment
from clipmachine.campaign.models import CampaignConfig, Moment
from clipmachine.campaign.post import (
    TITLE_MAX,
    keep_words,
    post_text,
    sentence_case,
    shorten,
    youtube_title,
)
from clipmachine.utils.wordlists import load_wordlists
from test_render import invented_lists

ACRONYMS = ["NBA", "NFL", "MPJ", "US", "UK"]


def cfg(**changes: Any) -> CampaignConfig:
    data = base_campaign()
    data["post"]["prefix"] = {"tiktok": "@demo", "youtube": "@demo-show"}
    data.update(changes)
    return CampaignConfig.model_validate(data)


def mom(**fields: Any) -> Moment:
    return Moment.model_validate(moment(**fields))


def lines(text: str) -> list[str]:
    assert text.endswith("\n") and "\r" not in text
    return text.rstrip("\n").split("\n")


# --- sentence case -----------------------------------------------------------------------------


def test_sentence_case_lowers_everything_but_the_first_letter() -> None:
    assert sentence_case("THE BLUE LANTERN NEVER WENT OUT") == "The blue lantern never went out"
    assert sentence_case("already Fine, thanks.") == "Already fine, thanks."
    assert sentence_case("  spaced   out ") == "Spaced out"
    assert sentence_case("") == ""


def test_kept_words_keep_their_spelling_whatever_the_case_around_them() -> None:
    keep = ["Zorb1e", "QXV", *ACRONYMS]
    assert sentence_case("WHY ZORB1E LEFT THE NBA", keep) == "Why Zorb1e left the NBA"
    assert sentence_case("ZORB1E AND MPJ IN THE US?", keep) == "Zorb1e and MPJ in the US?"
    assert sentence_case("qxv, EXPLAINED", keep) == "QXV, explained"  # punctuation stays


def test_a_kept_phrase_of_several_words_is_matched_whole() -> None:
    keep = ["Michael Zorb"]
    assert sentence_case("MICHAEL ZORB SPEAKS", keep) == "Michael Zorb speaks"
    assert sentence_case("MICHAEL SPEAKS", keep) == "Michael speaks"  # not the phrase


def test_an_all_caps_hook_with_a_name_and_an_acronym_comes_out_correctly_cased() -> None:
    keep = keep_words(cfg(names=["Zorb1e"], allowed=["Frob"]), ACRONYMS)
    assert youtube_title("ZORB1E SAYS THE NFL IS DONE WITH FROB", keep) == (
        "Zorb1e says the NFL is done with Frob"
    )


def test_keep_words_gathers_names_targets_allowed_and_the_app_list_once() -> None:
    words = keep_words(cfg(names=["Zorb1e", "QXV"], allowed=["frob", "Zorb1e"]), ["NBA", "nba"])
    assert words == ["Zorb1e", "QXV", "frob", "NBA"]  # spelling targets Zorb1e and QXV repeat


# --- the title length ----------------------------------------------------------------------------


def test_shorten_cuts_at_a_word_boundary_and_never_passes_the_limit() -> None:
    long = " ".join(f"word{n}" for n in range(30))  # 198 characters
    short = shorten(long)
    assert len(short) <= TITLE_MAX and short.endswith("...") and not short.endswith(" ...")
    assert short[:-3].strip() in long and short[:-3].split()[-1].startswith("word")
    assert shorten("fits") == "fits" and shorten("x" * 100) == "x" * 100
    assert len(shorten("y" * 150)) == TITLE_MAX


def test_a_long_hook_makes_a_title_of_at_most_100_characters() -> None:
    hook = " ".join(["AN", "INVENTED", "HOOK"] * 12)
    title = youtube_title(hook, [])
    assert len(title) <= TITLE_MAX and title.startswith("An invented hook")


# --- the text per platform -----------------------------------------------------------------------


def test_tiktok_and_instagram_are_prefix_caption_hashtags_closer() -> None:
    campaign = cfg()
    text, hits = post_text(campaign, mom(platform="tiktok"), "closer one", None)
    assert lines(text) == ["@demo says something invented.", "closer one"] and hits == []
    text, _ = post_text(campaign, mom(platform="instagram"), "closer two", None)
    assert lines(text) == ["says something invented.", "closer two"]  # no instagram prefix
    tagged = cfg()
    tagged.post.hashtags.extend(["#one", "#two"])
    text, _ = post_text(tagged, mom(platform="tiktok"), "closer one", None)
    assert lines(text) == ["@demo says something invented.", "#one #two", "closer one"]


def test_youtube_is_title_blank_description_hashtags_closer() -> None:
    campaign = cfg()
    campaign.post.hashtags.append("#one")
    text, hits = post_text(campaign, mom(platform="youtube"), "closer three", None)
    assert lines(text) == [
        "TITLE: An invented hook",
        "",
        "@demo-show says something invented.",
        "#one",
        "closer three",
    ]
    assert hits == []


def test_the_youtube_title_keeps_names_and_acronyms(project: Path) -> None:
    campaign = cfg(names=["Zorb1e"])
    keep = keep_words(campaign, ["NBA"])
    text, _ = post_text(
        campaign, mom(platform="youtube", hook="ZORB1E ON THE NBA"), "closer one", None, keep=keep
    )
    assert lines(text)[0] == "TITLE: Zorb1e on the NBA"


def test_the_title_prefix_and_caption_are_masked_but_not_the_closer(project: Path) -> None:
    invented_lists(project)
    lists = load_wordlists(project)
    campaign = cfg()
    campaign.post.hashtags.append("#blorp")
    text, hits = post_text(
        campaign,
        mom(platform="youtube", hook="WHY BLORP MATTERS", caption="says blorp."),
        "blorp closer",
        lists,
    )
    assert lines(text) == [
        "TITLE: Why b*** matters",
        "",
        "@demo-show says b***.",
        "#blorp",
        "blorp closer",
    ]
    assert [h.word for h in hits] == ["blorp", "blorp"]


def test_the_title_is_sentence_cased_after_every_sentence_end() -> None:
    # found in Batch A: "You can't find yourself. you build it." (S22), "honesty? you're" (S54)
    assert sentence_case("YOU CAN'T FIND IT. YOU BUILD IT.") == "You can't find it. You build it."
    assert sentence_case("too much? you're out! (yes) 'really?' sure") == (
        "Too much? You're out! (Yes) 'really?' Sure"
    )
    assert sentence_case("WAIT... WHAT") == "Wait... what"  # an ellipsis is not an end
    assert sentence_case("THE U.S. TEAM") == "The u.s. team"  # nor an abbreviation
    assert sentence_case("NO. ZORB1E WON", ["Zorb1e"]) == "No. Zorb1e won"


@pytest.mark.parametrize("platform", ["tiktok", "instagram", "youtube"])
def test_the_closer_is_always_the_last_line(platform: str) -> None:
    text, _ = post_text(cfg(), mom(platform=platform), "the closer", None)
    assert lines(text)[-1] == "the closer"
