async def test_preview_renders_with_sample_context(api):
    r = await api.post("/templates/preview", json={"template": "{{ room_name }} / {{ sender }}"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"rendered", "error", "variables"}
    assert body["error"] is None and body["variables"] == ["room_name", "sender"]
    assert body["rendered"]


async def test_preview_context_override(api):
    r = await api.post(
        "/templates/preview",
        json={"template": "Hi {{ room_name }}", "context": {"room_name": "Team"}},
    )
    assert r.json()["rendered"] == "Hi Team"


async def test_preview_errors_are_data(api):
    syntax = (await api.post("/templates/preview", json={"template": "a\n{% if %}"})).json()
    assert syntax["rendered"] is None and syntax["error"]["line"] == 2
    undefined = (await api.post("/templates/preview", json={"template": "{{ nope }}"})).json()
    assert undefined["rendered"] is None and undefined["variables"] == ["nope"]
    assert undefined["error"]["message"]


async def test_preview_requires_template(api):
    r = await api.post("/templates/preview", json={})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
