import pytest
from pg_fixtures import personal_store, postgres_database


@pytest.fixture
def pg_database():
    with postgres_database() as database:
        yield database


@pytest.fixture
def pg_store(pg_database):
    return personal_store(pg_database)
