import re

from app.i18n import translator
from app.i18n_ko import KO
from app.roles import Role

from .conftest import csrf_of
from .i18n_keys import all_keys


def test_every_ui_string_has_a_korean_translation():
    missing = sorted(all_keys() - KO.keys())
    assert not missing, f"Missing Korean translations: {missing}"


def test_placeholders_match():
    for key, value in KO.items():
        assert set(re.findall(r"{(\w+)}", key)) == set(re.findall(r"{(\w+)}", value)), key


def test_translator_falls_back_and_formats():
    ko, en = translator("ko"), translator("en")
    assert ko("Dashboard") == "대시보드"
    assert en("Dashboard") == "Dashboard"
    assert ko("not a key") == "not a key"
    assert ko("Message sent to {n} machine(s).", n=3) == "장치 3대에 메시지를 보냈습니다."


def test_language_detection_and_switch(client):
    # Defaults to English, Accept-Language picks Korean.
    assert "Initial setup" in client.get("/setup").text
    r = client.get("/setup", headers={"Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"})
    assert "초기 설정" in r.text and 'lang="ko"' in r.text
    r = client.get("/setup", headers={"Accept-Language": "en-US"})
    assert "Initial setup" in r.text

    # Explicit choice is stored in a cookie and wins over the header.
    r = client.get("/lang/ko?next=/setup", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    assert "초기 설정" in client.get("/setup", headers={"Accept-Language": "en-US"}).text
    client.get("/lang/en?next=/setup")
    assert "Initial setup" in client.get("/setup").text
    # Open redirects are refused.
    assert client.get("/lang/ko?next=//evil.example", follow_redirects=False).headers["location"] == "/"
    assert client.get("/lang/xx", follow_redirects=False).status_code == 404


def test_korean_pages_and_flash(client, world, db):
    ex = world.tenancy("example.com")
    dev = world.dept(ex, "developers")
    world.user("root", role=Role.SUPER_ADMIN, machines=())
    world.user("john", dev)
    client.get("/lang/ko")
    r = client.post("/login", data={"csrf_token": csrf_of(client.get("/login").text), "tenancy": "example.com",
                                    "username": "john", "password": "password123"})
    page = r.text
    assert "대시보드" in page and "빠른 시작" in page and "사용자" in page

    # A policy error is shown translated, including the role name inside it.
    from app.models import User
    john = db.query(User).filter_by(username="john").one()
    r = client.post("/messages/compose", data={
        "csrf_token": csrf_of(client.get("/messages/compose").text),
        "machine_id": john.machines[0].id, "to": "*:*@developers:example.com", "body": "x"})
    assert r.status_code == 403
    assert "이 테넌시에서 부서 브로드캐스트는 부서 모더레이터 이상만 할 수 있습니다." in r.text

    for path in ["/", "/messages", "/messages/compose", "/boards", "/boards/developers", "/account",
                 f"/machines/{john.machines[0].id}"]:
        assert client.get(path).status_code == 200, path


def test_api_errors_stay_english(client, world):
    ex = world.tenancy("example.com")
    dev = world.dept(ex, "developers")
    world.user("john", dev)
    r = client.post("/api/v1/messages", json={"to": "*:*@developers:example.com", "body": "x"},
                    headers={**world.auth("john:laptop"), "Accept-Language": "ko"})
    assert r.status_code == 403
    assert r.json()["detail"] == "Department broadcasts require at least Department Moderator in this tenancy."


def test_skill_documents_are_served(client):
    r = client.get("/skill.md")
    assert r.status_code == 200 and r.text.startswith("---\nname: agents-community")
    assert client.get("/llms.txt").text == r.text
    assert "POST /messages" in client.get("/skill/reference.md").text
    assert "def main" in client.get("/skill/community.py").text
