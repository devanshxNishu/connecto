"""connect.py decisions that need no browser: what is refused before a page is even opened."""
import pytest

from connecto import connect


class NoPage:
    """A page that fails the test on first use: nothing may be opened or read."""

    def __getattr__(self, name):
        pytest.fail(f"the page was used ({name}) for an address that must never be opened")


@pytest.mark.parametrize("address", [
    "https://www.linkedin.com/in/.\\direct-two/", "https://www.linkedin.com/in/x\\..\\..\\mynetwork\\",
    "https://www.linkedin.com/in/%2e%2e/", "https://www.linkedin.com/company/acme/", "https://evil.example/in/jane/",
    "", "javascript:alert(1)",
])
def test_an_address_that_is_not_a_plain_profile_is_refused_without_opening_anything(address):
    outcome = connect.process_profile(NoPage(), address, "Hi", 300, dry_run=True)
    assert outcome.status == "error" and "not a plain LinkedIn profile address" in outcome.detail


# ----------------------------------------------------------------------------- whose page is this
@pytest.mark.parametrize("a,b,expected", [
    ("Jane Doe", "Jane Doe", True), ("jane doe", "JANE DOE", True), ("Jane Doe", "Jane Doe - Founder at Acme", True),
    ("Jane Doe (she/her)", "Jane Doe", True), ("Jane  Doe", "Jane Doe", True), ("Jane Doe", "Jane Doe,", True),
    ("Al", "Alan", False), ("Alan", "Al", False), ("Jane Doe", "Jane Doers", False), ("Jane", "Janet", False),
    ("Jane Doe", "Somebody Else", False), ("", "Jane", False), ("J", "J", False), ("Jane Doe", "", False),
    ("Zoë Müller", "ZOË MÜLLER | LinkedIn", True), ("Ｊane Doe", "Jane Doe", True),            # a full-width letter folds to a plain one
    (None, "Jane", False), ("Jane", None, False),
])
def test_same_name_matches_a_name_with_something_after_it_and_nothing_else(a, b, expected):
    assert connect.same_name(a, b) is expected


class TitledPage:
    def __init__(self, title=None, boom=False):
        self._title, self._boom = title, boom

    def title(self):
        if self._boom:
            raise RuntimeError("page is gone")
        return self._title


@pytest.mark.parametrize("title,owner", [
    ("(3) Jane Doe | LinkedIn", "Jane Doe"), ("Jane Doe | LinkedIn", "Jane Doe"), ("(12+) Jane Doe - Founder | LinkedIn", "Jane Doe - Founder"),
    ("Jane Doe", "Jane Doe"), ("", ""), (None, ""), ("| LinkedIn", ""), ("LinkedIn", "LinkedIn"),
])
def test_page_owner_is_the_name_in_the_title(title, owner):
    assert connect.page_owner(TitledPage(title)) == owner
    assert connect.page_owner(TitledPage(boom=True)) == ""


def test_without_a_name_in_the_title_there_is_no_header_and_so_nothing_to_click():
    class Untouchable(TitledPage):
        def locator(self, *a, **k):
            pytest.fail("the page was searched although it did not say whose it is")

    assert connect.top_card(Untouchable("")) is None


@pytest.mark.parametrize("url,login,checkpoint", [
    ("https://www.linkedin.com/login", True, False), ("https://www.linkedin.com/login?session_redirect=%2Ffeed", True, False),
    ("https://www.linkedin.com/uas/login", True, False), ("https://www.linkedin.com/authwall?trk=x", True, False),
    ("https://www.linkedin.com/signup/cold-join", True, False), ("https://www.linkedin.com/checkpoint/challenge/abc", False, True),
    ("https://www.linkedin.com/checkpoint", False, True), ("https://www.linkedin.com/challenge/xyz", False, True),
    ("https://www.linkedin.com/in/login/", False, False), ("https://www.linkedin.com/in/checkpoint/", False, False),
    ("https://www.linkedin.com/in/login-guide/", False, False), ("https://www.linkedin.com/in/checkpoint-charlie/", False, False),
    ("https://www.linkedin.com/in/jane/?next=/login", False, False), ("https://www.linkedin.com/in/jane/#/checkpoint/x", False, False),
    ("https://www.linkedin.com/feed/", False, False), ("https://www.linkedin.com/loginfo", False, False), ("", False, False),
    ("not a url at all", False, False), ("http://[::1", False, False), (None, False, False),
])
def test_login_and_checkpoint_pages_are_recognised_by_their_path_alone(url, login, checkpoint):
    from connecto.sel import is_checkpoint_url, is_login_url
    assert is_login_url(url) is login and is_checkpoint_url(url) is checkpoint


# ----------------------------------------------------------------------------- has the window gone away
class FakePage:
    def __init__(self, closed=False, raises=False):
        self.closed, self.raises = closed, raises

    def is_closed(self):
        if self.raises:
            raise RuntimeError("connection lost")
        return self.closed


class TargetClosedError(Exception):
    pass


def test_a_closed_window_is_noticed_with_or_without_an_exception():
    from connecto.browser import browser_closed
    assert browser_closed(FakePage(closed=True)) is True
    assert browser_closed(FakePage(raises=True)) is True                                  # cannot even ask: gone
    assert browser_closed(FakePage(), None) is False
    assert browser_closed(FakePage(), RuntimeError("something else timed out")) is False
    assert browser_closed(FakePage(), TargetClosedError("x")) is True
    for text in ("Target page, context or browser has been closed", "Target closed", "Page crashed", "Browser has disconnected"):
        assert browser_closed(FakePage(), RuntimeError(text)) is True, text
