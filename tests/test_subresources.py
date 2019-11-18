import pathlib

import pytest

from spinta.testing.utils import get_error_context

@pytest.mark.models(
    'backends/mongo/subitem',
    'backends/postgres/subitem',
)
def test_get_subresource(model, app):
    app.authmodel(model, ['insert', 'getone', 'hidden_subobj_getone'])

    resp = app.post(f'/{model}', json={'_data': [
        {
            '_op': 'insert',
            '_type': model,
            'scalar': '42',
            'subarray': [{
                'foo': 'foobarbaz',
            }],
            'subobj': {
                'subprop': 'foobar123',
            },
            'hidden_subobj': {
                'hidden': 'secret',
            }
        }
    ]})

    assert resp.status_code == 200, resp.json()
    id_ = resp.json()['_data'][0]['_id']

    resp = app.get(f'/{model}/{id_}/subarray')
    assert resp.status_code == 400
    assert get_error_context(resp.json(), "UnavailableSubresource", ["prop", "prop_type"]) == {
        'prop': 'subarray',
        'prop_type': 'array',
    }

    resp = app.get(f'/{model}/{id_}/scalar')
    assert resp.status_code == 400
    assert get_error_context(resp.json(), "UnavailableSubresource", ["prop", "prop_type"]) == {
        'prop': 'scalar',
        'prop_type': 'string',
    }

    resp = app.get(f'/{model}/{id_}/subobj')
    assert resp.status_code == 200
    assert resp.json() == {
        'subprop': 'foobar123',
    }

    resp = app.get(f'/{model}/{id_}/hidden_subobj')
    assert resp.status_code == 200
    assert resp.json() == {
        'hidden': 'secret',
    }


@pytest.mark.models(
    'backends/mongo/subitem',
    'backends/postgres/subitem',
)
def test_subresource_scopes(model, app):
    app.authmodel(model, ['insert'])

    resp = app.post(f'/{model}', json={'_data': [
        {
            '_op': 'insert',
            '_type': model,
            'scalar': '42',
            'subarray': [{
                'foo': 'foobarbaz',
            }],
            'subobj': {
                'subprop': 'foobar123',
            },
            'hidden_subobj': {
                'hidden': 'secret',
            }
        }
    ]})
    assert resp.status_code == 200, resp.json()
    id_ = resp.json()['_data'][0]['_id']

    # try to GET subresource without specific subresource or model scope
    resp = app.get(f'/{model}/{id_}/subobj')
    assert resp.status_code == 403

    # try to GET subresource without specific subresource scope,
    # but with model scope
    app._scopes = []
    app.authmodel(model, ['getone'])
    resp = app.get(f'/{model}/{id_}/subobj')
    assert resp.status_code == 200
    assert resp.json() == {
        'subprop': 'foobar123',
    }

    # try to GET subresource without model scope,
    # but with specific subresource scope
    app._scopes = []
    app.authmodel(model, ['subobj_getone'])
    resp = app.get(f'/{model}/{id_}/subobj')
    assert resp.status_code == 200
    assert resp.json() == {
        'subprop': 'foobar123',
    }

    # try to GET subresource without specific hidden subresource or model scope
    app._scopes = []
    resp = app.get(f'/{model}/{id_}/hidden_subobj')
    assert resp.status_code == 403

    # try to GET subresource without specific hidden subresource scope,
    # but with model scope
    app._scopes = []
    app.authmodel(model, ['getone'])
    resp = app.get(f'/{model}/{id_}/hidden_subobj')
    assert resp.status_code == 403

    # try to GET subresource without model scope,
    # but with specific hidden subresource scope
    app._scopes = []
    app.authmodel(model, ['hidden_subobj_getone'])
    resp = app.get(f'/{model}/{id_}/hidden_subobj')
    assert resp.status_code == 200
    assert resp.json() == {
        'hidden': 'secret',
    }


@pytest.mark.models(
    'backends/mongo/subitem',
    'backends/postgres/subitem',
)
def test_get_subresource_file(model, app, tmpdir):
    app.authmodel(model, ['insert', 'getone', 'pdf_update', 'pdf_getone'])

    resp = app.post(f'/{model}', json={'_data': [
        {
            '_op': 'insert',
            '_type': model,
            'scalar': '42',
            'subarray': [{
                'foo': 'foobarbaz',
            }],
            'subobj': {
                'subprop': 'foobar123',
            },
            'hidden_subobj': {
                'hidden': 'secret',
            }
        }
    ]})
    assert resp.status_code == 200, resp.json()
    id_ = resp.json()['_data'][0]['_id']

    pdf = pathlib.Path(tmpdir) / 'report.pdf'
    pdf.write_bytes(b'REPORTDATA')

    resp = app.put(f'/{model}/{id_}/pdf:ref', json={
        'content_type': 'application/pdf',
        'filename': str(pdf),
    })
    assert resp.status_code == 200

    resp = app.get(f'/{model}/{id_}/pdf')
    assert resp.status_code == 200
    # XXX: is this how file subresource GET should work?
    assert resp.content == b'REPORTDATA'
