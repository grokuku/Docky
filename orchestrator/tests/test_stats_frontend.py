"""LOT 3 (frontend) delivery checks.

Non-régression d'intégration : le dashboard référence bien le module
``stats.js`` et la brique est servie par StaticFiles. Le protocole WS
lui-même (auth cookie JWT, subscribe → snapshot, sample, unsubscribe) est
déjà couvert par ``test_stats_routes.py``.
"""


def test_dashboard_includes_stats_client(auth_client):
    resp = auth_client.get("/dashboard")
    assert resp.status_code == 200
    text = resp.text
    # Le module client est chargé après les autres.
    assert "/static/js/stats.js" in text
    # Contrôle d'intervalle + indicateur de flux présents.
    assert 'id="stats-interval-select"' in text
    assert 'id="stats-live-indicator"' in text


def test_stats_client_script_served(orchestrator_client):
    resp = orchestrator_client.get("/static/js/stats.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers.get("content-type", "")
    assert "window.DockyStats" in resp.text
    assert "/api/stats/stream" in resp.text


def test_dashboard_requires_auth(orchestrator_client):
    resp = orchestrator_client.get("/dashboard", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
