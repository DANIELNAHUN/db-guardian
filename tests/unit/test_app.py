def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_database_file_created(client, settings):
    assert settings.db_path.exists()
