"""API organization metadata: tags, filtering, and exact-version bulk updates."""

from camoufox_pm.config import get_settings


async def create(client, name, **kwargs):
    response = await client.post(
        "/api/v1/profiles", json={"name": name, "generate_fingerprint": False, **kwargs}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_tags_create_edit_clear_and_stale_version_are_public(client):
    profile = await create(client, "Tagged", tags=[" Work ", "WORK", "Client A"])
    assert profile["tags"] == ["work", "client a"]
    edited = await client.put(
        f"/api/v1/profiles/{profile['id']}",
        json={"tags": ["New"], "row_version": profile["row_version"]},
    )
    assert edited.status_code == 200 and edited.json()["tags"] == ["new"]
    stale = await client.put(
        f"/api/v1/profiles/{profile['id']}",
        json={"tags": [], "row_version": profile["row_version"]},
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_write"
    cleared = await client.put(
        f"/api/v1/profiles/{profile['id']}",
        json={"tags": [], "row_version": edited.json()["row_version"]},
    )
    assert cleared.json()["tags"] == []


async def test_tag_and_group_filters_are_applied_before_paging_and_search_matches_ids(client):
    a = await create(client, "Alpha", tags=["work", "client"], group="group-a")
    await create(client, "Beta", tags=["work"], group="group-a")
    await create(client, "Gamma", tags=["work", "client"], group="group-b")
    response = await client.get(
        "/api/v1/profiles",
        params=[("tags", "WORK"), ("tags", "client"), ("group", "group-a"), ("per_page", 1)],
    )
    body = response.json()
    assert body["total"] == 1 and body["profiles"][0]["id"] == a["id"]
    assert not body["has_next"]
    assert (
        await client.get(
            "/api/v1/profiles", params={"tag": "work", "group": "group-a", "per_page": 1, "page": 2}
        )
    ).json()["total"] == 2
    assert (await client.get("/api/v1/profiles", params={"search": a["id"]})).json()["profiles"][0][
        "id"
    ] == a["id"]
    invalid = await client.get("/api/v1/profiles", params={"tag": "x" * 33})
    assert invalid.status_code == 422


async def test_bulk_tag_edit_reports_partial_failure_and_keeps_other_metadata(client):
    first = await create(client, "Fresh", tags=["keep"])
    second = await create(client, "Changed", tags=["old"])
    await client.put(
        f"/api/v1/profiles/{second['id']}",
        json={"notes": "changed", "row_version": second["row_version"]},
    )
    result = await client.post(
        "/api/v1/profiles/tags",
        json={
            "profiles": [
                {"id": first["id"], "row_version": first["row_version"]},
                {"id": second["id"], "row_version": second["row_version"]},
            ],
            "add": ["WORK"],
            "remove": [],
        },
    )
    assert result.status_code == 200
    body = result.json()["data"]
    assert body["updated"][0]["tags"] == ["keep", "work"]
    assert body["skipped"][0]["id"] == second["id"]
    current = (await client.get(f"/api/v1/profiles/{second['id']}")).json()
    assert current["notes"] == "changed" and current["tags"] == ["old"]
    assert current["browser_settings"] == second["browser_settings"]


async def test_bulk_tag_routes_require_existing_auth(client, monkeypatch):
    monkeypatch.setenv("CPM_API_KEY", "organization-key")
    get_settings.cache_clear()
    try:
        assert (
            await client.post("/api/v1/profiles/tags", json={"profiles": [], "add": []})
        ).status_code == 401
    finally:
        get_settings.cache_clear()
