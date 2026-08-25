"""Who may open this app, and who may change what is in it.

The rule being pinned down here is the one that is easy to get backwards: a
whitelist that cannot be found, cannot be read or has nobody in it means
*nobody edits*. Every other reading of an empty list hands the dataset to
whoever asks.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import access as access_mod
from app import main as main_mod
from conftest import ANNOTATION, EDITOR_ID, OUTSIDER_ID, make_wav

SEGMENT = {"speaker": "speaker_0", "start": "00:00:01", "end": "00:00:02"}
PAYLOAD = {"segments": [SEGMENT], "audioStart": 0, "audioEnd": 6.0}


# ------------------------------------------------------------------ the file


def test_a_list_is_ids_and_optional_names(tmp_path):
    path = tmp_path / "list.csv"
    path.write_text(
        "client_id,user_name\n"
        f"{EDITOR_ID},Duy\n"
        f"{OUTSIDER_ID},\n"
    )
    roster = access_mod.read_roster(path)
    assert roster.name_of(EDITOR_ID) == "Duy"
    assert OUTSIDER_ID in roster
    # Present, just unnamed. The name decides nothing.
    assert roster.name_of(OUTSIDER_ID) is None


def test_comments_blank_lines_and_a_missing_header_are_all_fine(tmp_path):
    path = tmp_path / "list.csv"
    path.write_text(f"# who may edit\n\n{EDITOR_ID}\n\n")
    roster = access_mod.read_roster(path)
    assert list(roster.names) == [EDITOR_ID]
    assert roster.problems == ()


def test_an_id_matches_whatever_case_it_was_typed_in(tmp_path):
    path = tmp_path / "list.csv"
    path.write_text(f"  {EDITOR_ID.upper()}  , Duy \n")
    roster = access_mod.read_roster(path)
    assert EDITOR_ID in roster
    assert roster.name_of(EDITOR_ID) == "Duy"


def test_a_line_that_is_not_an_id_is_reported_not_swallowed(tmp_path):
    """A typo that silently grants nothing is the failure worth surfacing."""
    path = tmp_path / "list.csv"
    path.write_text(f"client_id,user_name\nnot an id!,Duy\n{EDITOR_ID},\n")
    roster = access_mod.read_roster(path)
    assert EDITOR_ID in roster
    assert roster.problems == ("not an id!",)


def test_a_missing_file_is_an_empty_list_not_a_crash(tmp_path):
    roster = access_mod.read_roster(tmp_path / "nope.csv")
    assert roster.exists is False and roster.names == {}


def test_the_file_is_re_read_when_it_changes(tmp_path):
    """An admin adding a colleague should not have to restart the server."""
    path = tmp_path / "list.csv"
    path.write_text("client_id,user_name\n")
    access_mod.forget_rosters()
    assert EDITOR_ID not in access_mod.roster(path)
    path.write_text(f"client_id,user_name\n{EDITOR_ID},Duy\n")
    assert EDITOR_ID in access_mod.roster(path)


# ---------------------------------------------------------------- the verdict


def _decide(client_id, allowed=(), blocked=()):
    return access_mod.decide(
        client_id,
        whitelist=access_mod.Roster(None, True, {i: "" for i in allowed}),
        blacklist=access_mod.Roster(None, True, {i: "" for i in blocked}),
    )


def test_only_a_listed_browser_may_edit():
    assert _decide(EDITOR_ID, allowed=[EDITOR_ID]).may_edit is True
    assert _decide(OUTSIDER_ID, allowed=[EDITOR_ID]).may_edit is False


def test_an_empty_whitelist_lets_nobody_edit():
    assert _decide(EDITOR_ID).may_edit is False


def test_a_request_with_no_id_at_all_may_not_edit():
    assert _decide(None, allowed=[EDITOR_ID]).may_edit is False


def test_the_blacklist_beats_the_whitelist():
    """On both lists means blocked: the block was added later and was meant."""
    verdict = _decide(EDITOR_ID, allowed=[EDITOR_ID], blocked=[EDITOR_ID])
    assert verdict.blocked is True and verdict.may_edit is False


def test_reading_is_never_a_change():
    assert access_mod.changes_data("GET", "/api/entries") is False
    assert access_mod.changes_data("HEAD", "/") is False


def test_anything_that_is_not_plainly_a_read_counts_as_a_change():
    assert access_mod.changes_data("PUT", "/api/entries/x") is True
    assert access_mod.changes_data("DELETE", "/api/entries/x") is True
    assert access_mod.changes_data("POST", "/api/entries") is True
    # A route nobody has written yet is guarded before it is written.
    assert access_mod.changes_data("POST", "/api/whatever-comes-next") is True


def test_parsing_an_upload_writes_nothing_so_anyone_may():
    assert access_mod.changes_data("POST", "/api/parse") is False


# -------------------------------------------------------------------- the app


def test_a_viewer_can_read_everything(viewer, uploaded):
    assert viewer.get("/api/entries").json()["count"] == 1
    assert viewer.get(f"/api/entries/{uploaded}").status_code == 200
    assert viewer.get("/").status_code == 200


def test_a_viewer_cannot_save_over_somebody_elses_work(viewer, dataset, uploaded):
    before = (dataset / f"{uploaded}.txt").read_text()
    response = viewer.put(f"/api/entries/{uploaded}", json=PAYLOAD)
    assert response.status_code == 403
    assert response.json()["detail"]["access"] == "readonly"
    assert (dataset / f"{uploaded}.txt").read_text() == before


def test_a_viewer_cannot_delete(viewer, dataset, uploaded):
    assert viewer.delete(f"/api/entries/{uploaded}").status_code == 403
    assert (dataset / f"{uploaded}.txt").is_file()


def test_a_viewer_cannot_file_a_new_entry(viewer, dataset):
    response = viewer.post(
        "/api/entries",
        data={"payload": __import__("json").dumps(PAYLOAD)},
        files={"file": ("a.wav", make_wav(), "audio/wav")},
    )
    assert response.status_code == 403
    assert not list(dataset.glob("*.txt"))


def test_a_viewer_may_still_read_an_uploaded_annotation(viewer):
    """It writes nothing, and refusing it would only confuse the refusal."""
    response = viewer.post(
        "/api/parse", files={"file": ("a.txt", ANNOTATION.encode(), "text/plain")})
    assert response.status_code == 200


def test_an_editor_may_do_all_of_it(client, dataset, uploaded):
    assert client.put(f"/api/entries/{uploaded}", json=PAYLOAD).status_code == 200
    assert client.delete(f"/api/entries/{uploaded}").status_code == 200


def test_adding_a_line_grants_editing_without_a_restart(viewer, whitelist, uploaded):
    assert viewer.put(f"/api/entries/{uploaded}", json=PAYLOAD).status_code == 403
    whitelist.write_text(f"client_id,user_name\n{OUTSIDER_ID},Newcomer\n")
    assert viewer.put(f"/api/entries/{uploaded}", json=PAYLOAD).status_code == 200


def test_losing_the_whitelist_locks_editing_rather_than_opening_it(client, whitelist,
                                                                   uploaded):
    whitelist.unlink()
    assert client.put(f"/api/entries/{uploaded}", json=PAYLOAD).status_code == 403


# ------------------------------------------------------------------- blocking


def test_a_blacklisted_browser_gets_no_page_and_no_data(client, blacklist, uploaded):
    blacklist.write_text(f"client_id,user_name\n{EDITOR_ID},Ex-colleague\n")
    assert client.get("/").status_code == 403
    assert client.get("/api/entries").status_code == 403
    assert client.get(f"/api/entries/{uploaded}/audio").status_code == 403
    assert client.get("/static/js/app.js").status_code == 403


def test_the_block_page_is_a_page_and_the_api_says_why(client, blacklist):
    blacklist.write_text(f"client_id,user_name\n{EDITOR_ID},\n")
    page = client.get("/")
    assert "not available" in page.text
    assert client.get("/api/access").json()["detail"]["access"] == "blocked"


def test_the_page_load_is_blocked_by_the_cookie_alone(client, blacklist):
    """The header only reaches the API; the document request carries a cookie.

    ``client`` is here for the settings it points at, not for its own headers:
    this browser sends nothing but the cookie.
    """
    blacklist.write_text(f"client_id,user_name\n{EDITOR_ID},\n")
    bare = TestClient(main_mod.app, cookies={access_mod.COOKIE: EDITOR_ID})
    assert bare.get("/").status_code == 403


# --------------------------------------------------------------------- /access


def test_access_tells_a_viewer_the_two_things_that_would_fix_it(viewer, whitelist):
    body = viewer.get("/api/access").json()
    assert body["role"] == "viewer"
    assert body["clientId"] == OUTSIDER_ID
    assert body["whitelistPath"] == str(whitelist)
    assert body["editorCount"] == 1


def test_access_greets_an_editor_by_the_name_in_the_file(client):
    body = client.get("/api/access").json()
    assert body["role"] == "editor" and body["userName"] == "Test Admin"


def test_access_surfaces_a_line_the_admin_got_wrong(client, whitelist):
    whitelist.write_text(f"client_id,user_name\n{EDITOR_ID},Duy\nnope!,Someone\n")
    assert client.get("/api/access").json()["problems"] == ["nope!"]


# ------------------------------------------------------------ every refresh


def test_a_refresh_sees_the_list_as_it_is_now(client, viewer, whitelist):
    """Both directions, with no restart: granted, then taken away again."""
    assert viewer.get("/api/access").json()["role"] == "viewer"
    whitelist.write_text(f"client_id,user_name\n{OUTSIDER_ID},Newcomer\n")
    assert viewer.get("/api/access").json()["role"] == "editor"
    whitelist.write_text("client_id,user_name\n")
    assert viewer.get("/api/access").json()["role"] == "viewer"
    # And the one that was there all along is gone with it.
    assert client.get("/api/access").json()["role"] == "viewer"


def test_the_answer_is_never_cached(client, blacklist):
    """A cached verdict outlives the file it came from. Both must revalidate."""
    assert client.get("/api/access").headers["cache-control"] == "no-store"
    assert client.get("/").headers["cache-control"] == "no-store"
    blacklist.write_text(f"client_id,user_name\n{EDITOR_ID},\n")
    assert client.get("/").headers["cache-control"] == "no-store"
