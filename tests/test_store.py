import pytest


def test_schema_loader(context, app):
    country, = context.push([
        {
            'type': 'country',
            'code': 'lt',
            'title': 'Lithuania',
        },
    ])
    org, = context.push([
        {
            'type': 'org',
            'title': 'My Org',
            'govid': '0042',
            'country': country['id'],
        },
    ])

    assert country == {
        'id': country['id'],
        'type': 'country',
    }
    assert org == {
        'id': org['id'],
        'type': 'org',
    }

    app.authorize(['spinta_getone'])

    resp = app.get(f'/org/{org["id"]}')
    data = resp.json()
    revision = data['revision']
    assert data == {
        'id': org['id'],
        'govid': '0042',
        'title': 'My Org',
        'country': country['id'],
        'type': 'org',
        'revision': revision,
    }

    resp = app.get(f'/country/{country["id"]}')
    data = resp.json()
    revision = data['revision']
    assert data == {
        'id': country['id'],
        'code': 'lt',
        'title': 'Lithuania',
        'type': 'country',
        'revision': revision,
    }


# FIXME: postgres nested objects
# @pytest.mark.models(
#     'backends/mongo/report',
#     'backends/postgres/report',
# )
@pytest.mark.models(
    'backends/mongo/report',
)
def test_nested(model, app):
    app.authmodel(model, ['insert', 'getone'])

    resp = app.post(f'/{model}', json={
        'type': model,
        'notes': [{'note': 'foo'}]
    })
    assert resp.status_code == 201
    data = resp.json()
    id_ = data['id']
    revision = data['revision']

    data = app.get(f'/{model}/{id_}').json()
    assert data['id'] == id_
    assert data['type'] == model
    assert data['revision'] == revision
    assert data['notes'] == [{
        'note': 'foo',
        'note_type': None,
        'create_date': None
    }]
